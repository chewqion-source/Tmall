from pathlib import Path
import datetime
import shutil


base = Path("/opt/tmall-dashboard/data")
backup = base / "backups"
backup.mkdir(parents=True, exist_ok=True)
stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

pairs = [
    (Path("/tmp/douyin_report.xlsx"), "2026年盲盒抖音日报表.xlsx"),
    (Path("/tmp/qianfan_report.xlsx"), "2026年盲盒千帆日报表.xlsx"),
]

for source, name in pairs:
    target = base / name
    if target.exists():
        backup_name = f"{target.stem}_before_0920_upload_{stamp}{target.suffix}"
        shutil.copy2(target, backup / backup_name)
    shutil.copy2(source, target)
    print(f"updated {target} {target.stat().st_size}")
