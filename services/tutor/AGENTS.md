# services/tutor — tutoring domain & assessment authority (score 8)

## OVERVIEW
Tutor sessions for minors: lesson catalog, turn policy, and server-minted HMAC-signed learning outcomes. The one service whose learning outcomes can never be client-decided.

## WHERE TO LOOK
| Task | Location |
|------|----------|
| Session/practice records | `domain.py` — `LessonTask`, `PracticeSession`, `TutorAggregateCommit` (canonical sha256) |
| Assessment authority | `authority.py` — `FencedAssessmentAuthority`, `TutorEvidenceGate`, `TutorScoringRubric`, `sign_assessment_payload` |
| Lesson catalog | `catalog.py` — 50 English scenario cards + homework companion (scenario cards, not an answer bank) |
| Turn policy | `turn_policy.py` — stuck reframing, stale-turn fail-closed |
| Style prompts | `prompts.py` — `TUTOR_STYLE`, `focus_style` |
| Main consumers | `agent/src/{tutor_session,prompts,mode_policy_client}.py`, `control_api/app/mode_policy.py`, `routes/{interaction,session}.py`, `guardian/postgres_store.py` (10 symbols) |

## CONVENTIONS
- Outcomes minted by server: HMAC-signed `TutorAssessmentEvidence`, one-time opaque `assessment_id`, `TutorEvidenceGate` re-verifies signature + fence fields.
- Mastery: `MASTERY_MIN_CORRECTNESS=0.8` + `MASTERY_MIN_ATTEMPTS=2`; open-ended homework cards set `masterable=False`.
- Commits atomic: consume + session revision CAS + evidence + archive outbox in one `TutorAggregateCommit`.
- Stateful policy bounded: OrderedDict LRU, 512 sessions × 16 turns, `threading.Lock`.
- Records frozen `@dataclass(slots=True)` with `__post_init__` validation; bounds via `_required`/`_bounded`/`_non_negative`/`_utc` helpers (tz-aware UTC).
- `authority.py` exposes 28 names via `__all__`; `domain.py` has none (direct imports).
- Chinese user-facing strings (prompts, catalog titles) inside ASCII source; no package-level re-exports.

## ANTI-PATTERNS
- Never give answers or write submittable content; never mock/shame/label-grade; one correction per turn, one small step.
- First stuck turn → reframe without hint; second consecutive → minimal hint; stale/duplicate turn fails closed (`stale_tutor_turn_fail_closed`).
- `StudyProgress` is a rebuildable projection, never an authoritative grade or diagnosis.
- Catalog cards stay scenario cards — never turn them into an answer-bearing question bank.
- `TutorEvidenceGate` fence fields must stay complete (subject, actor, device, binding+version, session epoch, runtime profile, subject revision, policy receipt).
