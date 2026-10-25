#!/usr/bin/env python3
"""東横イン公式サイトの空室を監視し、空きが出たら GitHub Issue で知らせる（予約はしない）。

GitHub Actions から10分ごとに呼ばれる想定。1回の実行で次を行う。
  1. 監視期限を過ぎていたらワークフローを無効化して終わる
  2. 空室チェック用エージェント (Haiku) で、対象エリアの東横インの空室を読む
  3. 空きが出たら Issue を作って知らせる（すでに通知中なら、新しく空いたホテルだけコメントで追記）
  4. 通知中に全館満室へ戻ったら、Issue にコメントして閉じる

モード (--mode):
  watch  本番。空室の変化を Issue で知らせる（既定）
  check  空室チェックだけ。通知はしない
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "target.json"
MCP_CONFIG_PATH = ROOT / "config" / "mcp.json"
SCHEMA_PATH = ROOT / "schemas" / "vacancy_result.schema.json"
STATE_DIR = ROOT / "state"
STATE_PATH = STATE_DIR / "state.json"
OUT_DIR = ROOT / "out"

JST = dt.timezone(dt.timedelta(hours=9))

CHECKER_AGENT = "toyoko-vacancy-checker"
CHECKER_MODEL = os.environ.get("TW_CHECKER_MODEL", "haiku")
CHECKER_BUDGET_USD = 0.5
CHECKER_TIMEOUT_SEC = 7 * 60

LABEL_VACANCY = "toyoko-vacancy"
LABEL_NOTICE = "toyoko-notice"

ERROR_STREAK_NOTIFY = 6  # 10分 x 6 = 約1時間チェックに失敗し続けたら知らせる

IN_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"


class ConfigError(Exception):
    pass


class AgentError(Exception):
    pass


# ---------------------------------------------------------------------------
# 小物


def now_jst() -> dt.datetime:
    return dt.datetime.now(JST)


def log(msg: str) -> None:
    print(f"[{now_jst():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def warn(msg: str) -> None:
    if IN_ACTIONS:
        print(f"::warning::{msg}", flush=True)
    else:
        log(f"WARNING: {msg}")


def norm(text: str) -> str:
    """全角英数・全角記号などを半角にそろえる。"""
    return unicodedata.normalize("NFKC", text or "").strip()


def same_date(text: str, iso_date: str) -> bool:
    nums = re.findall(r"\d+", norm(text))
    if len(nums) < 3:
        return False
    try:
        return dt.date(*(int(n) for n in nums[:3])).isoformat() == iso_date
    except ValueError:
        return False


def write_summary(markdown: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(markdown + "\n")


def save_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            warn("state/state.json が壊れていたので作り直します")
    return {}


def nights(cfg: dict) -> int:
    return (dt.date.fromisoformat(cfg["checkout"]) - dt.date.fromisoformat(cfg["checkin"])).days


def stay_label(cfg: dict) -> str:
    ci, co = dt.date.fromisoformat(cfg["checkin"]), dt.date.fromisoformat(cfg["checkout"])
    return f"{ci.month}/{ci.day}→{co.month}/{co.day}・{cfg['adults']}名"


# ---------------------------------------------------------------------------
# GitHub（Actions 上でだけ使う）


def gh(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["gh", *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:2])} failed: {proc.stderr.strip()}")
    return proc


def run_url() -> str:
    return f"{os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}"


def create_issue(title: str, body: str, label: str) -> int | None:
    if not IN_ACTIONS:
        log(f"[通知] {title}\n{body}")
        return None
    gh("label", "create", LABEL_VACANCY, "--color", "0e8a16", "--description", "東横インの空室あり", "--force", check=False)
    gh("label", "create", LABEL_NOTICE, "--color", "fbca04", "--description", "空室監視からのお知らせ", "--force", check=False)
    args = ["issue", "create", "--title", title, "--body", f"{body}\n\n実行ログ: {run_url()}", "--label", label]
    owner = os.environ.get("GITHUB_REPOSITORY_OWNER")
    proc = gh(*args, *(["--assignee", owner] if owner else []), check=False)
    if proc.returncode != 0:
        # assignee を付けられない場合でも Issue だけは作る
        proc = gh(*args)
    log(f"Issue を作成しました: {title}")
    m = re.search(r"/issues/(\d+)", proc.stdout)
    return int(m.group(1)) if m else None


def comment_issue(number: int | None, body: str, close: bool = False) -> None:
    if not IN_ACTIONS or number is None:
        log(f"[通知コメント{'・クローズ' if close else ''}] #{number}\n{body}")
        return
    if close:
        gh("issue", "close", str(number), "--comment", body)
    else:
        gh("issue", "comment", str(number), "--body", body)


def find_open_vacancy_issue(state: dict) -> int | None:
    if not IN_ACTIONS:
        return state.get("open_issue")
    proc = gh("issue", "list", "--label", LABEL_VACANCY, "--state", "open", "--json", "number", "--limit", "1")
    issues = json.loads(proc.stdout or "[]")
    return issues[0]["number"] if issues else None


def disable_workflow() -> None:
    if not IN_ACTIONS:
        log("（ローカル実行なのでワークフローの無効化は省略）")
        return
    workflow_file = os.environ["GITHUB_WORKFLOW_REF"].split("@")[0].rsplit("/", 1)[-1]
    proc = gh("workflow", "disable", workflow_file, check=False)
    if proc.returncode == 0:
        log(f"ワークフロー {workflow_file} を無効化しました。")
    else:
        warn(f"ワークフローの無効化に失敗しました: {proc.stderr.strip()}")


# ---------------------------------------------------------------------------
# エージェント呼び出し


def run_checker(cfg: dict, hint_urls: list[str]) -> dict:
    claude = shutil.which("claude")
    if not claude:
        raise ConfigError("claude コマンドが見つかりません（npm i -g @anthropic-ai/claude-code）")
    payload = {k: cfg[k] for k in ("site_url", "checkin", "checkout", "rooms", "adults", "hotel_keywords", "area_hints")}
    payload["hint_urls"] = hint_urls
    prompt = ("次の条件で、東横イン公式サイトの空室を調べて、指定の JSON スキーマで返してください。予約はしないでください。"
              f"\n\n```json\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n```\n")
    cmd = [
        claude, "-p",
        "--agent", CHECKER_AGENT,
        "--model", CHECKER_MODEL,
        "--mcp-config", str(MCP_CONFIG_PATH),
        "--strict-mcp-config",
        "--allowedTools", "mcp__playwright",
        "--permission-mode", "dontAsk",
        "--output-format", "json",
        "--json-schema", SCHEMA_PATH.read_text(encoding="utf-8"),
        "--max-budget-usd", str(CHECKER_BUDGET_USD),
        "--no-session-persistence",
    ]
    started = time.monotonic()
    try:
        proc = subprocess.run(cmd, input=prompt, cwd=ROOT, capture_output=True, text=True, timeout=CHECKER_TIMEOUT_SEC)
    except subprocess.TimeoutExpired as e:
        raise AgentError(f"{CHECKER_AGENT} が {CHECKER_TIMEOUT_SEC} 秒以内に終わりませんでした") from e
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise AgentError(f"出力を読めませんでした (exit {proc.returncode}): {(proc.stderr or proc.stdout)[-2000:]}") from e
    models = ", ".join(result.get("modelUsage", {}).keys()) or CHECKER_MODEL
    log(f"{CHECKER_AGENT}: {time.monotonic() - started:.0f}秒, model={models}, turns={result.get('num_turns')}")
    output = result.get("structured_output")
    if not isinstance(output, dict) and not result.get("is_error"):
        output = json_from_text(str(result.get("result") or ""))
    if result.get("is_error") or not isinstance(output, dict):
        raise AgentError(f"結果を返しませんでした: {result.get('subtype')} {str(result.get('result'))[:1000]}")
    return output


def json_from_text(text: str) -> dict | None:
    """StructuredOutput を使わずに本文へ JSON を書いてきた場合の拾い上げ。必須キーがそろっていれば採用する。"""
    required = json.loads(SCHEMA_PATH.read_text(encoding="utf-8")).get("required", [])
    for block in re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S) + [text]:
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and all(k in data for k in required):
            return data
    return None


# ---------------------------------------------------------------------------
# 判定・通知の文面


def target_hotels(cfg: dict, check: dict) -> list[dict]:
    keywords = [norm(k) for k in cfg["hotel_keywords"]]
    return [h for h in check.get("hotels", []) if any(k in norm(h.get("name", "")) for k in keywords)]


def is_available(hotel: dict) -> bool:
    return hotel.get("availability") == "available" or any(r.get("available") for r in hotel.get("rooms", []))


def available_hotels(cfg: dict, check: dict) -> list[dict]:
    return [h for h in target_hotels(cfg, check) if is_available(h)]


def all_full(cfg: dict, check: dict) -> bool:
    """対象ホテルをすべて読めて、どこも満室だったときだけ True（unknown が混ざれば満室とは言わない）。"""
    hotels = target_hotels(cfg, check)
    return bool(hotels) and all(h.get("availability") == "full" and not is_available(h) for h in hotels)


def condition_mismatch(cfg: dict, check: dict) -> str | None:
    if not same_date(check.get("checkin_seen", ""), cfg["checkin"]):
        return f"チェックイン日 ({check.get('checkin_seen')}) が {cfg['checkin']} と一致しません"
    if check.get("nights_seen") not in (None, nights(cfg)):
        return f"泊数 ({check.get('nights_seen')}) が {nights(cfg)} 泊と一致しません"
    if check.get("adults_seen") not in (None, cfg["adults"]):
        return f"人数 ({check.get('adults_seen')}) が {cfg['adults']} 名と一致しません"
    return None


AVAILABILITY_JA = {"available": "空きあり", "full": "満室", "unknown": "読み取れず"}


def rooms_label(hotel: dict) -> str:
    rooms = [r for r in hotel.get("rooms", []) if r.get("available")]
    if not rooms:
        return "空きあり（部屋タイプは読み取れず）"
    return "、".join(f"{r['type']}（{r.get('status_raw', '')}"
                    f"{'・' + format(r['price_yen'], ',') + '円' if r.get('price_yen') else ''}）" for r in rooms)


def hotel_lines(hotels: list[dict]) -> str:
    return "\n".join(f"| {h['name']} | {rooms_label(h)} | {('[開く](' + h['url'] + ')') if h.get('url') else ''} |"
                     for h in hotels)


def vacancy_body(cfg: dict, hotels: list[dict], check: dict) -> str:
    others = [h for h in target_hotels(cfg, check) if h not in hotels]
    other_lines = "\n".join(f"- {h['name']}: {AVAILABILITY_JA.get(h.get('availability'), h.get('availability'))}" for h in others) or "- なし"
    return (
        f"東横イン（{'・'.join(cfg['hotel_keywords'])}周辺）に空室が出ました。\n\n"
        f"- 日程: {cfg['checkin']} チェックイン → {cfg['checkout']} チェックアウト\n"
        f"- 人数: 1室 大人{cfg['adults']}名\n\n"
        "| ホテル | 空いている部屋 | 空室ページ |\n|---|---|---|\n"
        f"{hotel_lines(hotels)}\n\n"
        f"**このシステムは予約しません。** 空室はすぐ埋まることがあるので、公式サイト（{cfg['site_url']}）で早めに予約してください。\n\n"
        f"<details><summary>そのほかの対象ホテル</summary>\n\n{other_lines}\n\n</details>\n\n"
        "満室に戻ったら、この Issue にコメントして自動で閉じます。"
    )


def report(cfg: dict, check: dict) -> None:
    hotels = target_hotels(cfg, check)
    log(f"チェックイン {check.get('checkin_seen')} / {check.get('nights_seen')}泊 / 大人{check.get('adults_seen')}名 / 対象 {len(hotels)} 館")
    for h in hotels:
        log(f"  {h['name']}  [{h.get('availability')}] {rooms_label(h) if h.get('availability') == 'available' else ''}")
    if check.get("notes"):
        log(f"  notes: {check['notes']}")
    rows = "\n".join(f"| {h['name']} | {AVAILABILITY_JA.get(h.get('availability'), h.get('availability'))} | {rooms_label(h) if is_available(h) else ''} |"
                     for h in hotels)
    write_summary(f"### 空室チェック {now_jst():%m/%d %H:%M} JST（{stay_label(cfg)}）\n\n"
                  f"| ホテル | 空室 | 空いている部屋 |\n|---|---|---|\n{rows}\n\n{check.get('notes', '')}\n")


# ---------------------------------------------------------------------------
# 本体


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=["watch", "check"], default="watch")
    args = parser.parse_args()

    if IN_ACTIONS and not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")):
        raise ConfigError("Secrets に CLAUDE_CODE_OAUTH_TOKEN（claude setup-token で発行）を設定してください")

    cfg = load_config()
    state = load_state()
    OUT_DIR.mkdir(exist_ok=True)
    log(f"mode={args.mode} {cfg['checkin']}→{cfg['checkout']} 1室{cfg['adults']}名 / {'・'.join(cfg['hotel_keywords'])}")

    if args.mode == "watch" and now_jst() >= dt.datetime.fromisoformat(cfg["stop_watching_at"]):
        log(f"監視期限 {cfg['stop_watching_at']} を過ぎたので終了します")
        if "period-ended" not in state.get("notices", []):
            create_issue("[東横イン] 空室の監視期間が終わりました",
                         f"{cfg['stop_watching_at']} を過ぎたので監視を終了しました。", LABEL_NOTICE)
            state.setdefault("notices", []).append("period-ended")
            save_json(STATE_PATH, state)
        disable_workflow()
        return 0

    stamp = f"{now_jst():%Y%m%d-%H%M}"
    try:
        check = run_checker(cfg, state.get("hint_urls", []))
    except AgentError as e:
        check = {"status": "error", "error": str(e), "hotels": []}
    save_json(OUT_DIR / f"check-{stamp}.json", check)
    if check.get("status") == "ok" and not target_hotels(cfg, check):
        check = {**check, "status": "error",
                 "error": f"名前に {'・'.join(cfg['hotel_keywords'])} を含むホテルが検索結果に見つかりませんでした: {check.get('notes', '')}"}

    if check.get("status") != "ok":
        state["error_streak"] = state.get("error_streak", 0) + 1
        warn(f"空室チェックに失敗しました（{state['error_streak']} 回連続）: {check.get('error')}")
        write_summary(f"### 空室チェック失敗\n\n{check.get('error')}\n")
        if args.mode == "watch" and state["error_streak"] == ERROR_STREAK_NOTIFY:
            create_issue("[東横イン] 空室チェックが続けて失敗しています",
                         f"{ERROR_STREAK_NOTIFY} 回続けて空室チェックに失敗しました。監視は続けています。\n\n"
                         f"直近のエラー:\n```\n{check.get('error')}\n```", LABEL_NOTICE)
        save_json(STATE_PATH, state)
        return 0 if args.mode == "watch" else 1
    state["error_streak"] = 0

    report(cfg, check)
    mismatch = condition_mismatch(cfg, check)
    if mismatch:
        warn(f"{mismatch}。今回は通知しません")
        save_json(STATE_PATH, state)
        return 0
    if check.get("search_urls"):
        state["hint_urls"] = check["search_urls"][:4]

    available = available_hotels(cfg, check)
    log("空室あり: " + (" / ".join(h["name"] for h in available) or "なし"))
    if args.mode == "check":
        save_json(STATE_PATH, state)
        return 0

    open_issue = find_open_vacancy_issue(state)
    if open_issue is None and state.get("open_issue"):
        # 通知した Issue を自分で閉じた（予約できた・見送った）なら、知らせ済みのホテルは満室に戻るまで再通知しない
        log(f"通知中の Issue #{state['open_issue']} が閉じられていました。知らせ済みのホテルは再通知しません")
        state["open_issue"] = None
    if not available and not all_full(cfg, check):
        log("読み取れなかったホテルがあるので、今回は通知の状態を変えません")
        save_json(STATE_PATH, state)
        return 0

    notified = set(state.get("notified", []))
    new = [h for h in available if norm(h["name"]) not in notified]
    if new and open_issue is None:
        title = f"[東横イン] 空室あり: {new[0]['name']}{' ほか' if len(new) > 1 else ''}（{stay_label(cfg)}）"
        state["open_issue"] = create_issue(title, vacancy_body(cfg, available, check), LABEL_VACANCY)
    elif new:
        comment_issue(open_issue, f"新たに空室が出ました（{now_jst():%m/%d %H:%M}）\n\n"
                                  "| ホテル | 空いている部屋 | 空室ページ |\n|---|---|---|\n" + hotel_lines(new))
    elif not available and open_issue is not None:
        comment_issue(open_issue, f"満室に戻りました（{now_jst():%m/%d %H:%M}）。また空きが出たら新しい Issue で知らせます。", close=True)
        state["open_issue"] = None
    # 知らせ済みから外すのは、満室と読めたホテルだけ（読み取れなかった回をはさんで同じ空室を二重に知らせない）
    full_now = {norm(h["name"]) for h in target_hotels(cfg, check) if h.get("availability") == "full" and not is_available(h)}
    state["notified"] = sorted({norm(h["name"]) for h in available} | (notified - full_now))
    save_json(STATE_PATH, state)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ConfigError as e:
        print(f"設定エラー: {e}", file=sys.stderr)
        if IN_ACTIONS:
            print(f"::error::{e}")
        sys.exit(2)
