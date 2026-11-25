# Commands: start, scan_complete, cancel, abort (return to base). Stamp uses synchronized wall time.
builtin_interfaces/Time stamp
string<=128 session_id
string<=128 assigned_callsign
string<=128 area_of_interest
string command
# start only: autonomous or supervised; home must use map, metres, quaternion.
string mode
geometry_msgs/PoseStamped home
