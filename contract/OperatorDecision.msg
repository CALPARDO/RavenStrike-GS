builtin_interfaces/Time stamp
string session_id
string mission_id
# Must match the current image or cycle_complete token; stale decisions are rejected.
string evidence_id
# approve/OK or retry/Clean Again. Discovery-only mode accepts approval, not retry.
string decision
