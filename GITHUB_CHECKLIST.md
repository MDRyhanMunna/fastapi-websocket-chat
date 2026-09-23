# GitHub Upload Checklist

Before pushing the repository:

- [ ] `main.py` is present
- [ ] `index.html` is present
- [ ] `requirements.txt` is present
- [ ] `.gitignore` is present
- [ ] `README.md` is present
- [ ] `chat.db` is **not** present
- [ ] database backup files are **not** present
- [ ] `.env` is **not** present
- [ ] `__pycache__` is **not** present
- [ ] real user uploads are **not** present
- [ ] no passwords, API keys, JWT secrets, or tokens are hardcoded
- [ ] screenshots contain no private data you do not want public
- [ ] choose a repository visibility: Public or Private
- [ ] optionally add a license

## First push

Create a new empty repository on GitHub, then run from this project folder:

```bash
git init
git add .
git status
git commit -m "Initial commit: FastAPI WebSocket chat application"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPOSITORY.git
git push -u origin main
```

## Later updates

```bash
git add .
git commit -m "Describe your changes"
git push
```

## If a private file was accidentally committed

Simply adding it to `.gitignore` does not remove it from Git history. Rotate any exposed secret immediately and remove sensitive data from repository history before making the repository public.
