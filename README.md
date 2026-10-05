# Pharma Case Triage & Routing

A digital worker that takes a captured inbound case (call, email, web form, chat), works out what kind of issue it is, applies SOP and regulatory rules, and routes it to Drug Safety (PV), Product Quality (QA), Medical Information (MI), or a human triage lead.

> Demo project: synthetic products, synthetic cases, and mocked downstream systems. The regulatory logic is simplified and is not regulatory advice.

## How it works

```
IntakeCase ─► Extract (Claude) ─► Ground ─► Rules ─► Route ─► Dispatch ─► Argus / TrackWise / Service Cloud (mock)
                                                         └──► Human triage lead (if review needed)
                every step ─► hash-chained audit log
```

| Step | Module | LLM? | What it does |
|---|---|---|---|
| Extract | `app/extraction.py` | Yes | Claude returns quote-backed facts: intents, patient and reporter, products and lot numbers, events and seriousness, complaint, medical question. |
| Ground | `app/grounding.py` | No | Drops any fact whose quote isn't in the transcript and records what was dropped. |
| Rules | `app/rules.py` | No | Minimum ICSR criteria (PV-MIN-01..04), seriousness, expectedness against reference safety information, reporting clock, missing-information checklists. |
| Route | `app/routing.py` | No | Routing matrix, confidence score, human-in-the-loop triggers. |
| Dispatch | `app/connectors.py` | No | One child record per destination, linked to the parent case and to each other. |
| Audit | `app/db.py` | No | Append-only, SHA-256 hash-chained audit log, verifiable with `GET /audit/verify`. |

### Design decisions

- **Claude extracts facts, and code makes every regulatory decision.** Validity, deadlines and destinations come from deterministic rules with stable IDs, so they can be unit-tested and cited in an audit.
- **Day 0 is `received_at`**: when the company first became aware of the information, not when triage ran.
- **Clock categories.** Post-marketing: serious and unlisted is a 15-day alert report; everything else goes to periodic reporting. Clinical trial: a fatal or life-threatening SUSAR is 7-day, any other SUSAR is 15-day. If the product can't be found in the catalog, the event is treated as unexpected. If the minimum criteria aren't met, the clock isn't set and follow-up is requested.
- **A potential AE always reaches PV,** even when it isn't a valid ICSR or the extractor didn't tag the AE intent. Missing information goes on a follow-up checklist rather than causing the case to be rerouted.
- **The confidence score is built from checks the system can make itself**: grounding failures, ambiguities the extractor reported, uncertain seriousness, unknown products and out-of-matrix intents. It never uses the model's self-reported confidence. The threshold is 0.90. Some conditions send the case to review whatever the score.
- **Cases are never dropped.** If extraction fails, the case goes to the human queue with status `failed`.
- Cases in review are **not** sent downstream until a triage lead signs off. The queue is sorted by earliest regulatory due date.

## Run it

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt      # Windows; use .venv/bin on macOS/Linux
cp .env.example .env                               # add ANTHROPIC_API_KEY

.venv/Scripts/python -m pytest -q                  # offline tests, no API calls
.venv/Scripts/python scripts/run_eval.py           # live eval on data/eval_cases.json (calls Claude)
.venv/Scripts/uvicorn app.main:app --reload        # API at http://127.0.0.1:8000/docs
```

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/cases` | Submit an intake case; returns the triage result |
| GET | `/cases?status=` | List cases, earliest due date first |
| GET | `/cases/{id}` | Case, extraction, result, downstream records |
| GET | `/cases/{id}/audit` | Audit trail for one case |
| POST | `/cases/{id}/review` | Triage lead approves or overrides (comment required) |
| GET | `/queues/human-triage` | Cases awaiting review |
| GET | `/queues/{destination}` | Records sent to `drug_safety`, `product_quality`, `medical_information` |
| GET | `/audit/verify` | Verify the audit hash chain |

## Not done yet

- Reviewer UI
- Real system connectors
- Authentication and e-signatures (needed for full 21 CFR Part 11)
- Follow-up workflow that restarts the clock when the minimum criteria are completed
- Duplicate detection across cases
- MedDRA coding
