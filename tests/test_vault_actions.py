"""Record lines of an Action's Notes: the current form below date headings and the earlier dated form."""

from __future__ import annotations

import re

from tkn_genai_chat_note.process_settings import DEFAULT_RECORD_LINE, ActionSections
from tkn_genai_chat_note.vault_actions import cited_sessions, notes_lines

S1 = "11111111-1111-4111-8111-111111111111"
S2 = "22222222-2222-4222-8222-222222222222"
S3 = "33333333-3333-4333-8333-333333333333"
S4 = "44444444-4444-4444-8444-444444444444"
S5 = "55555555-5555-4555-8555-555555555555"

BODY = f"""## 3. Gaps & Actions

- DECIDED (user): Not in Notes. 根拠: Claude Code {S5}

## 4. Notes

- 2026-11-08 決定（本人）：Earlier form. 根拠：Claude Code {S1}
- 2026-11-08 実施（Codex）：Same evidence. 根拠：同上

### 2026-11-09

- CHECKED (Claude Code): Looked at the folder。根拠: Claude Code {S2}（[開く](claude://claude.ai/epitaxy/local_x)）
- DONE (Claude Code): Made the Action。根拠: 同上
- DECIDED (user, via tkn-genai-chat-note): Keep it。根拠: Codex {S3}
- DECIDED(user):Without spaces。根拠:Codex {S4}
- A free note (draft): not a record. 根拠: Claude Code {S5}
- TODO (later): not a record either. 根拠: Claude Code {S5}

### Visible output

- https://example.com
"""


def test_both_record_forms_are_read_and_free_notes_are_not() -> None:
    heading = re.compile(ActionSections().notes)
    lines = notes_lines(BODY, heading)
    found, count = cited_sessions(lines, re.compile(DEFAULT_RECORD_LINE), ["同上"])
    assert found == {S1, S2, S3, S4}
    assert count == 6


def test_record_groups() -> None:
    record = re.compile(DEFAULT_RECORD_LINE)
    current = record.match(f"- DECIDED (user, via tkn-genai-chat-note): Keep it。根拠: Codex {S3}")
    assert current is not None
    assert current.group("date") is None
    assert (current.group("kind"), current.group("actor"), current.group("conclusion")) == (
        "DECIDED", "user, via tkn-genai-chat-note", "Keep it")
    assert current.group("evidence") == f"Codex {S3}"
    earlier = record.match(
        f"- 2026-11-08 決定（本人。tkn-genai-chat-noteが抽出）：Earlier form。根拠：Claude Code {S1}")
    assert earlier is not None
    assert (earlier.group("date"), earlier.group("kind"), earlier.group("actor")) == (
        "2026-11-08", "決定", "本人。tkn-genai-chat-noteが抽出")
    assert earlier.group("evidence") == f"Claude Code {S1}"
