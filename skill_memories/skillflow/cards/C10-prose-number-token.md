---
id: C10
type: number-format
trigger: prose/markdown summary must echo computed figures a grader substring-matches
source: Healthcare-Cost-Benefit-Analysis (harbor_reagentkit_bulk, harbor_syncpack_28v56)
---

# prose-number-token

TRIGGER — A markdown/prose summary must repeat computed figures, and graders commonly substring-match the standard-formatted number (thousands separators, two decimals).

TECHNIQUE
- Emit each graded number as an UNBROKEN `{:,.2f}` token, e.g. `-8,412.75`, `1,234.50`. The sign stays adjacent to the first digit.
- Do NOT insert a currency symbol or space between the sign and the digits. Writing `**-$8,412.75 USD**` puts `$` between `-` and `8`, so the contiguous substring `-8,412.75` never appears and the match fails — even though the number is correct.
- Bold/emphasis around the whole token is fine (`**-8,412.75**`); just keep the numeric characters contiguous. If you want a currency label, put it OUTSIDE the token (`-8,412.75 USD`), not inside.

SELF-CHECK — For each figure your summary must echo, build the exact `f'{v:,.2f}'` string and assert it is a literal substring of your finished text (no `$`, no space breaking sign-from-digits).

SOURCE: Healthcare-Cost-Benefit-Analysis — JSON passed; markdown failed because a currency symbol inserted between the sign and digits broke the formatted-number substring.
