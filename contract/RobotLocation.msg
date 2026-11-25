# One UGV per paired link. stamp is the screened GNSS measurement time (UTC).
builtin_interfaces/Time stamp
# WGS84 antenna position, degrees multiplied by 10^7.
int32 latitude_e7
int32 longitude_e7
# Body-forward bearing clockwise from true north, in hundredths of a degree.
uint16 HEADING_UNKNOWN=65535
uint16 heading_cdeg
