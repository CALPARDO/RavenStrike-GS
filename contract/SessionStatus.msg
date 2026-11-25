builtin_interfaces/Time stamp
string session_id
# standby, working, returning, completed, halted, canceled
string state
string active_mission_id
string[] queued_mission_ids
bool scanning_complete
uint32 completed_tasks
uint32 unsuccessful_tasks
string notes
# Opaque ID of the onboard JSON session log, empty until finalized.
string log_reference
