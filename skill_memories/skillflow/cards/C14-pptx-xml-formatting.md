---
id: C14
type: xml-fidelity
trigger: .pptx restyle — set font pt/#hex/bold-off, widen caption box, auto-numbered bullets
source: PPT-Formatting-Optimization (transit-platform, wildlife-field-guide, archive-photo caption-cleanup)
---

# pptx-xml-formatting

TRIGGER — A .pptx task requires setting a font size/name/color, clearing bold/italic, widening a caption box, or adding auto-numbered bullets — and grading reads the raw OOXML nodes.

TECHNIQUE
- Set font size/name/bold/italic on EACH text RUN's `a:rPr`, not paragraph defaults. A paragraph-level set leaves the run's `sz` unset -> grader reads `got 0, expected 1600`.
- Font name must be written to BOTH `<a:latin>` AND `<a:ea>` typefaces. **This is the single most-missed gate**: `python-pptx`'s `run.font.name = "Arial"` sets ONLY `<a:latin>` and leaves `<a:ea>` at the template's original (`assert 'Lucida Grande' == 'Arial'` fails on an otherwise-perfect run). python-pptx exposes no `ea` setter, so write the XML node yourself on every styled run, e.g.:
  ```python
  from pptx.oxml.ns import qn
  rPr = run._r.get_or_add_rPr()
  for tag in ("a:latin", "a:ea", "a:cs"):
      el = rPr.find(qn(tag))
      if el is None:
          el = rPr.makeelement(qn(tag), {}); rPr.append(el)
      el.set("typeface", "Arial")   # the task's target font
  ```
  Do the same loop for `a:ea` on EVERY run you restyle, not just the first.
- Clear bold AND italic explicitly (`b="0"`, `i="0"`); don't assume one implies the other.
- Widen the caption shape's `xfrm`/`ext cx` to >= 90% of the FILE'S ACTUAL slide width (read `presentation.slide_width`, don't assume 12192000). Test wants `container_w >= slide_width*0.9` (`assert 3657600 >= 10972800` fails).
- Use real `<a:buAutoNum>` for auto-numbered bullets, not plain bullet chars or narration claiming a numbered list.

SELF-CHECK — Reopen the pptx zip and read the XML: assert each run's `rPr` has the right `sz`/latin+ea name/`b=0`/`i=0`, the shape `cx >= 0.9 * slide_width`, and a `a:buAutoNum` node is present. Do not trust the high-level python-pptx call.

SOURCE: PPT-Formatting-Optimization hard-zero family — run `rPr` size 0, `ea` typeface unchanged, box 3657600 < 10972800, and missing `buAutoNum` all failed at the XML level despite passing text edits.
