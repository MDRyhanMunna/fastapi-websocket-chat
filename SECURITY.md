# Security Notes

## Do not commit private runtime data

The following files from a local development copy should **not** be published:

- `chat.db`
- `chat*.db` backups
- `.env`
- `__pycache__/`
- real files inside `uploads/`

The included `.gitignore` prevents these from being added in normal Git usage.

A local SQLite database may contain usernames, email addresses, password hashes, messages, room membership data, and upload metadata. Even though passwords are hashed, the database should still be treated as private application data.

## Secret key

Set `CHAT_SECRET_KEY` to a strong random value in the process environment before deployment.

The development fallback in `main.py` is not intended to protect a public production service.

## Uploaded files

This application currently exposes stored uploads through FastAPI `StaticFiles`. Anyone who knows an upload URL may be able to request it. A production private-chat system should serve private attachments through authenticated authorization checks or private object storage.

## Production hardening ideas

- HTTPS and secure WebSockets (`wss://`)
- Rate limiting
- Login brute-force protection
- Email verification / password recovery
- Refresh-token or session revocation strategy
- Input/security testing
- Malware scanning for uploaded files
- Content Security Policy and additional HTTP security headers
- PostgreSQL for production persistence
- Redis or another shared broker for multi-process/multi-server WebSocket fan-out
- Automated backups and database migrations
- Application logging and monitoring
