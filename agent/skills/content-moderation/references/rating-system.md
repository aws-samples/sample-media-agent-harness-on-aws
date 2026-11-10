# General Rating System

Use ONLY this fictional system. It does NOT correspond to any real-world
rating body (MPAA, BBFC, PEGI, FSK, NICAM, ACB). Never output G / PG /
PG-13 / R / NC-17 or any real-world level.

| Rating | Description |
|---|---|
| All Ages | Suitable for all audiences. No concerning material. |
| Child 7+ | Mild themes or situations not suitable for very young children. |
| Teen 13+ | Moderate material: mild language, thematic elements, brief intense scenes. |
| Adult 18+ | Strong material: graphic violence, explicit language, sexual content, substance use. |

The final report MUST include:
- **Suggested Rating**: one of "Adult 18+", "Teen 13+", "Child 7+", "All Ages"
- **Justification**: detailed explanation, referencing specific timestamps/findings
- **Elements**: comma-separated list of contributing elements

## Output constraint (hard)

`suggested_rating` MUST be EXACTLY one of these four literal strings — nothing
else is valid output:

    "All Ages" | "Child 7+" | "Teen 13+" | "Adult 18+"

FORBIDDEN: inventing labels or codes such as "GR-T", "Teen & Up", "Moderate",
"PG-13", or any composite/parenthetical variant. A severity descriptor
("moderate", "mild") may appear ONLY in the per-category rows, NEVER as the
rating value. (Observed defect: a run emitted "GR-T — Teen & Up (Moderate)",
which is not a member of the enum.)