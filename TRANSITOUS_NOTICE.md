# Transitous and OpenStreetMap attribution

The route-planning feature uses the public Transitous MOTIS API:
https://transitous.org/api/

Transitous API usage is limited to this open-source, non-commercial application.
Requests include the application name, version, and service URL in the User-Agent.
The application caches geocoding and route results and enforces a rolling 24-hour
limit of 600 route-planning requests, below Transitous' published 720-request
coordination threshold. Automated polling and scraping are not used.

Map and geocoding data are provided by OpenStreetMap contributors.
OpenStreetMap data is available under the Open Database License (ODbL).
Attribution: https://www.openstreetmap.org/copyright

Transitous sources and attribution details:
https://transitous.org/sources/

Transitous is a community service provided on a best-effort basis. Route
availability, timetables, and real-time information may be incomplete or delayed.
Always verify important travel information with the relevant transport operator.

Geocoding uses the public Nominatim service. Requests are user-initiated,
cached for seven days, and rate-limited to at most one request per second.
