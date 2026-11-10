# Visual moderation search queries

Run `search_content` once per row against the indexed keyframe collection.
Text queries are matched against the visual embeddings of the frames.

| # | Category | Query |
|---|---|---|
| 1 | VIOLENCE (weapons) | gun weapon firearm shooting muzzle flash bullet hole handgun rifle |
| 2 | VIOLENCE (fighting) | fighting assault punch kick blood injury gore wound |
| 3 | VIOLENCE (destruction) | explosion crash destruction fire burning wreckage |
| 4 | SUBSTANCE (alcohol) | beer bottle alcohol drinking wine liquor glass bar |
| 5 | SUBSTANCE (drugs) | drug package cocaine brick wrapped plastic powder pills cash money bag |
| 6 | SUBSTANCE (smoking) | cigarette smoking cigar smoke |
| 7 | HARM | dangerous driving vehicle armed reckless chase speeding |
| 8 | SEXUAL | nudity naked sexual suggestive intimate undressed |
| 9 | FRIGHTENING | horror scary disturbing dark threatening menacing blood |
| 10 | IMITABLE | dangerous stunt criminal technique weapon handling knife trick |
| 11 | DISCRIMINATION | offensive symbol hate symbol |

Call shape:
```yaml
search_content:
  collection: "<asset_id>-moderation-shot"
  search_term: "<query from table>"
```


## Score → confidence
- Score > 0.7 → HIGH confidence flag
- Score 0.4–0.7 → MEDIUM confidence flag
- Score < 0.4 → discard

LANGUAGE is detected from the transcript (sg7), not visual search.

## Transcript scan (sg7) targets
- Profanity / expletives (exact timecodes + verbatim quotes)
- Threats of violence, hate speech, discriminatory language
- Drug / substance references in dialogue
- Death threats, menacing language, stalking references

Per finding: exact timestamp (from diarization), verbatim quote, category
(LANGUAGE / VIOLENCE if threat / SUBSTANCE if drug reference / FRIGHTENING if
menacing), confidence (1.0 for verbatim profanity, lower for implied).