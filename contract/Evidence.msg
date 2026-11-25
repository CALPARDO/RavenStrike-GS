builtin_interfaces/Time stamp
string session_id
string mission_id
string evidence_id
# before, after, not_found; cycle_complete is a review token with empty image fields.
string kind
builtin_interfaces/Time image_stamp
string image_frame
string sha256
# Opaque record ID; resolve with GetEvidence on onboard ROS/IP, not a host path.
string image_reference
# Reduced JPEG for telemetry/operator review, at most 12288 bytes.
sensor_msgs/CompressedImage preview
string preview_sha256

# RTM v6: original JPEG filename and measured geographic search-area center.
string filename
geographic_msgs/GeoPoint location
geometry_msgs/Point search_center
float64 search_radius
