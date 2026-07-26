from __future__ import annotations

import tkinter as tk
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tkinter import filedialog, messagebox

from license_admin.license_generator import create_license, write_license
from license_admin.signing_identity import SigningIdentityError, load_signing_private_key


def main() -> None:
    root = tk.Tk()
    try:
        load_signing_private_key()
    except SigningIdentityError as exc:
        root.withdraw()
        messagebox.showerror("无法启动许可证签发工具", str(exc))
        root.destroy()
        return
    root.title("离线许可证签发")
    fields: dict[str, tk.Entry] = {}
    labels = (("客户名称", "customer"), ("设备申请码", "device"), ("许可证编号", "license_id"), ("生效时间", "not_before"), ("到期时间", "expires_at"))
    for row, (label, key) in enumerate(labels):
        tk.Label(root, text=label).grid(row=row, column=0, padx=8, pady=6, sticky="w")
        entry = tk.Entry(root, width=52)
        entry.grid(row=row, column=1, padx=8, pady=6)
        fields[key] = entry
    now = datetime.now(timezone.utc).replace(microsecond=0)
    fields["not_before"].insert(0, now.isoformat())
    fields["expires_at"].insert(0, (now + timedelta(days=365)).isoformat())

    def generate() -> None:
        try:
            value = create_license(customer_name=fields["customer"].get(), device_code=fields["device"].get(), license_id=fields["license_id"].get(), not_before=fields["not_before"].get(), expires_at=fields["expires_at"].get())
            path = filedialog.asksaveasfilename(defaultextension=".license", filetypes=(("许可证文件", "*.license"),))
            if path:
                write_license(value, Path(path))
                messagebox.showinfo("完成", f"许可证已生成：{path}")
        except Exception as exc:
            messagebox.showerror("无法生成许可证", str(exc))

    tk.Button(root, text="生成许可证", command=generate).grid(row=len(labels), column=1, padx=8, pady=12, sticky="e")
    root.mainloop()


if __name__ == "__main__":
    main()
