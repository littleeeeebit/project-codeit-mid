The JupyterHub OAuth client (`auth.Login`, `auth.Hub`):
- `GET /api/auth/login` redirects to the hub's authorize URL with a state cookie scoped to `/api/auth/`. A visit through another host is first moved to the registered redirect host.
- `GET /api/auth/callback` compares the state in constant time. `Login.sign_in(code)` exchanges the code at the hub's token endpoint, reads `/hub/api/user` and gives a session only to usernames in `BIDMATE_ALLOWED_USERS`.
- `POST /api/auth/logout` ends the session.

Sessions live in server memory; README states a 12-hour HttpOnly cookie. With `BIDMATE_LOCAL_MEMBER`, every call runs as that one name with no hub.