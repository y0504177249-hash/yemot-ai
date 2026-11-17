# Transitous and OpenStreetMap attribution

The map feature uses Transitous' public MOTIS 2 API:
- Route planning: https://api.transitous.org/api/v6/plan
- One-shot place lookup: https://api.transitous.org/api/v1/geocode

This application is open-source and non-commercial. Requests are user-initiated;
there is no automatic polling, scraping, or type-ahead autocomplete. Place
lookups are cached for seven days. Route results are cached for five minutes.
A persistent rolling 24-hour counter limits route-planning calls to 600,
below Transitous' published 720-request coordination threshold. If persistent
usage accounting is unavailable, route requests fail closed.

Every API request sends a descriptive User-Agent containing the application
name, client version, and service URL. Attribution to Transitous and the
OpenStreetMap contributors is displayed in the website and route responses.

Transitous source and attribution information:
https://transitous.org/sources/

Transitous privacy information:
https://transitous.org/privacy/
Transitous may log IP address, request time, requested route/place queries,
and User-Agent for up to two days. Do not enter confidential information in
place fields. Route and timetable data are best-effort and may be incomplete
or delayed; verify critical travel details with the relevant operator.

OpenStreetMap data is available under the Open Database License (ODbL).
Attribution: https://www.openstreetmap.org/copyright
