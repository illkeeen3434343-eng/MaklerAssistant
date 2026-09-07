"""
Extract every user-facing string from the MaklerAssistant bot.

Scans for strings that actually reach a Telegram user:
  msg.answer(...)          bot.send_message(...)      msg.answer_document(...)
  ask.ask_text/ask_choice  button labels (BTN_*)      raise PublishError/LoginError
  ask_photos prompts       OtpRejected / error text   KeyboardButton(text=...)

Outputs a CSV the user can translate, plus a readable report.
"""
from __future__ import annotations

import ast
import csv
import re
from pathlib import Path

FILES = ["bina_bot.py", "bina_core.py", "bina_publish.py", "ask_broker.py", "users.py"]

# calls whose string arguments are shown to the user
USER_CALLS = {
    "answer", "send_message", "reply", "answer_document", "answer_photo",
    "send_photo", "ask_text", "ask_choice", "ask_photos", "_deny",
}
USER_EXCEPTIONS = {"PublishError", "LoginError", "OtpRejected"}

AZ_CHARS = set("əğışçöüƏĞİŞÇÖÜ")
AZ_WORDS = {"və", "üçün", "edin", "yoxdur", "seçin", "göndər", "elan", "nömrə",
            "giriş", "sessiya", "istifadəçi", "qeyd", "təsdiq", "ləğv", "xəta"}


def looks_azerbaijani(s: str) -> bool:
    if any(ch in AZ_CHARS for ch in s):
        return True
    low = s.lower()
    return any(w in low for w in AZ_WORDS)


def clean(s: str) -> str:
    """Collapse whitespace so the CSV stays one row per message."""
    return re.sub(r"\s+", " ", s).strip()


def is_ui_text(s: str) -> bool:
    """Filter out selectors, urls, keys and other non-message strings."""
    if len(s.strip()) < 3:
        return False
    bad_markers = (
        "http://", "https://", "data-cy", "data-stat", "[role=", "input[",
        "button[", "div[", "span[", ".sc-", "#sms-", "#phone-", "()", "=>",
        "document.", "querySelector", "_next", "utf-8", "%s",
    )
    if any(b in s for b in bad_markers):
        return False
    # pure identifiers / snake_case keys
    if re.fullmatch(r"[a-z0-9_\-:.]+", s.strip()):
        return False
    # placeholder-only strings like "{} {}" or "⚠️ {}" carry no words
    letters = re.sub(r"\{\}|[^\w]", "", s, flags=re.UNICODE)
    if len(letters) < 3:
        return False
    return True


def area_of(row) -> str:
    """Group a message by the part of the bot it belongs to."""
    ctx = (row["context"] or "").lower()
    f = row["file"]
    if row["kind"] == "button":
        if ctx.startswith("btn_a_") or "admin" in ctx:
            return "1. Menyu düymələri — Admin"
        if ctx.startswith("btn_s_"):
            return "1. Menyu düymələri — Sessiyalar"
        return "1. Menyu düymələri — Əsas"
    if "admin" in ctx:
        return "5. Admin paneli"
    if "report" in ctx or "contact" in ctx:
        return "6. Əlaqə / Şikayət"
    if "debug" in ctx:
        return "8. Debug (yalnız admin)"
    if f == "bina_core.py":
        return "2. Giriş / OTP"
    if any(k in ctx for k in ("session", "phone", "login", "logout", "switch",
                              "remove", "s_new", "s_list")):
        return "3. Giriş və Sessiyalar"
    if f == "bina_publish.py" or "publish" in ctx or "wizard" in ctx or "pick" in ctx:
        return "4. Yeni elan (sehrbaz)"
    if "ads" in ctx:
        return "7. Elanlarım"
    return "9. Digər"


def collect(path: Path):
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    lines = src.splitlines()
    found = []

    def add(node, text, kind, ctx):
        text = clean(text)
        if not is_ui_text(text):
            return
        found.append({
            "file": path.name,
            "line": getattr(node, "lineno", 0),
            "context": ctx,
            "kind": kind,
            "text": text,
        })

    def enclosing_func(node):
        return getattr(node, "_func", "")

    # tag nodes with their enclosing function name
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for sub in ast.walk(fn):
                if not hasattr(sub, "_func"):
                    sub._func = fn.name

    def render(node):
        """Rebuild a full message template from an expression.

        f-strings become 'text {} text', concatenations are joined, and
        conditional expressions yield both branches. Returns None when the
        value isn't a literal message (e.g. "\\n".join(lines)).
        """
        if isinstance(node, ast.Constant):
            return node.value if isinstance(node.value, str) else None
        if isinstance(node, ast.JoinedStr):
            parts = []
            for v in node.values:
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    parts.append(v.value)
                else:
                    parts.append("{}")
            return "".join(parts)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = render(node.left), render(node.right)
            if left is None or right is None:
                return None
            return left + right
        if isinstance(node, ast.IfExp):          # "a" if cond else "b"
            out = [x for x in (render(node.body), render(node.orelse)) if x]
            return " || ".join(out) if out else None
        return None

    def strings_in(node):
        """Full message templates for an expression (preferred), else nothing."""
        whole = render(node)
        if whole is not None:
            return [whole]
        # Fall back to standalone literals only for containers (lists/tuples of
        # button labels etc.), never for pieces of an unrenderable expression.
        if isinstance(node, (ast.List, ast.Tuple)):
            out = []
            for el in node.elts:
                r = render(el)
                if r:
                    out.append(r)
            return out
        return []

    for node in ast.walk(tree):
        # button constants:  BTN_X = "..."
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id.startswith("BTN_"):
                    for s in strings_in(node.value):
                        add(node, s, "button", t.id)

        if isinstance(node, ast.Call):
            fname = ""
            if isinstance(node.func, ast.Attribute):
                fname = node.func.attr
            elif isinstance(node.func, ast.Name):
                fname = node.func.id

            if fname in USER_CALLS:
                for a in node.args:
                    for s in strings_in(a):
                        add(node, s, fname, enclosing_func(node))
                for kw in node.keywords:
                    if kw.arg in ("text", "caption", "prompt"):
                        for s in strings_in(kw.value):
                            add(node, s, fname, enclosing_func(node))

            if fname in USER_EXCEPTIONS:
                for a in node.args:
                    for s in strings_in(a):
                        add(node, s, "error", enclosing_func(node))

            if fname == "KeyboardButton":
                for kw in node.keywords:
                    if kw.arg == "text":
                        for s in strings_in(kw.value):
                            add(node, s, "button", enclosing_func(node))

            if fname == "InlineKeyboardButton":
                for kw in node.keywords:
                    if kw.arg == "text":
                        for s in strings_in(kw.value):
                            add(node, s, "button", enclosing_func(node))

        if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
            f = node.exc.func
            nm = f.id if isinstance(f, ast.Name) else getattr(f, "attr", "")
            if nm in USER_EXCEPTIONS:
                for a in node.exc.args:
                    for s in strings_in(a):
                        add(node, s, "error", enclosing_func(node))

    return found


def main():
    rows = []
    for f in FILES:
        p = Path(f)
        if p.exists():
            rows.extend(collect(p))

    # de-duplicate on text, keep first location
    seen, uniq = set(), []
    for r in rows:
        key = r["text"]
        if key in seen:
            continue
        seen.add(key)
        r["language"] = "AZ" if looks_azerbaijani(r["text"]) else "EN"
        r["area"] = area_of(r)
        uniq.append(r)

    uniq.sort(key=lambda r: (r["area"], r["language"], r["file"], r["line"]))
    for i, r in enumerate(uniq, 1):
        r["id"] = i

    # ---- CSV for translating ----
    with open("bot_messages.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=[
            "id", "area", "language", "file", "line", "kind",
            "text", "translation_az"])
        w.writeheader()
        for r in uniq:
            w.writerow({
                "id": r["id"], "area": r["area"], "language": r["language"],
                "file": r["file"], "line": r["line"], "kind": r["kind"],
                "text": r["text"],
                # pre-fill already-Azerbaijani ones so you only edit the rest
                "translation_az": r["text"] if r["language"] == "AZ" else "",
            })

    # ---- readable grouped report ----
    with open("BOT-MESSAGES.md", "w", encoding="utf-8") as fh:
        en_total = sum(1 for r in uniq if r["language"] == "EN")
        fh.write("# Bot mesajları — tərcümə siyahısı\n\n")
        fh.write(f"Cəmi **{len(uniq)}** mesaj · tərcümə gözləyən (EN): "
                 f"**{en_total}** · artıq AZ: **{len(uniq) - en_total}**\n\n")
        fh.write("`{}` = dəyişən (rəqəm, ad, nömrə). Tərcümədə də saxlayın, "
                 "yerini dəyişə bilərsiniz.\n\n")
        fh.write("Sütunlar: **ID** · mövcud mətn · (fayl:sətir)\n\n---\n\n")
        current = None
        for r in uniq:
            if r["area"] != current:
                current = r["area"]
                fh.write(f"\n## {current}\n\n")
            flag = "🔴 EN" if r["language"] == "EN" else "🟢 AZ"
            fh.write(f"- **{r['id']}.** {flag} — `{r['text']}`  \n"
                     f"  <sub>{r['file']}:{r['line']} · {r['kind']}</sub>\n")
        fh.write("\n")

    en = [r for r in uniq if r["language"] == "EN"]
    print(f"total unique messages : {len(uniq)}")
    print(f"  still ENGLISH       : {len(en)}   <-- need translation")
    print(f"  already Azerbaijani : {len(uniq) - len(en)}")
    print("\nby area:")
    areas = {}
    for r in uniq:
        areas.setdefault(r["area"], [0, 0])
        areas[r["area"]][0] += 1
        if r["language"] == "EN":
            areas[r["area"]][1] += 1
    for a in sorted(areas):
        tot, e = areas[a]
        print(f"  {a:38} {tot:>3} mesaj ({e} EN)")
    print("\nwrote bot_messages.csv and BOT-MESSAGES.md")


if __name__ == "__main__":
    main()
