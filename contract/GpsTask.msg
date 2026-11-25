builtin_interfaces/Time stamp
string session_id
string mission_id
string assigned_callsign
uint8 hazard_level
# WGS84 latitude/longitude degrees, ellipsoid altitude metres. No datum default.
geographic_msgs/GeoPoint location
# Desired map-frame robot heading, radians. Object orientation is separate.
float64 yaw
