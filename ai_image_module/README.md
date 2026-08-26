# AI Image Module

This folder is the source of truth for the website AI image generator.

Deployment rule:

1. Edit files in this folder.
2. Run syntax checks.
3. Commit changes to Git.
4. Deploy only `ai_image_app.py` to the server.

The deploy script updates only:

```text
/opt/tmall-dashboard/current/ai_image_app.py
```

It does not change the dashboard, upload page, KOC page, nginx config, data files, or service files.

## Check

```powershell
python -m py_compile ai_image_module\ai_image_app.py
```

## Deploy

```powershell
.\ai_image_module\deploy_ai_image_module.ps1
```
