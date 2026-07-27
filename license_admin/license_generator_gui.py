from __future__ import annotations

import tkinter as tk
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tkinter import filedialog, messagebox

from license_admin.license_generator import create_license, write_license
from license_admin.signing_identity import SigningIdentityError, load_signing_private_key, private_key_path


def main() -> None:
    root = tk.Tk()
    root.title("离线许可证签发")
    fields: dict[str, tk.Entry] = {}
    status_var = tk.StringVar(value="")

    try:
        load_signing_private_key()
    except SigningIdentityError as exc:
        status_var.set(
            "未找到可用签发私钥，工具已启动但暂不能签发。\n"
            f"请将私钥放到：{private_key_path()}\n"
            f"错误信息：{exc}"
        )

    labels = (
        ("客户名称", "customer"),
        ("设备申请码", "device"),
        ("许可证编号", "license_id"),
        ("生效时间", "not_before"),
        ("到期时间", "expires_at"),
    )
    for row, (label, key) in enumerate(labels):
        tk.Label(root, text=label).grid(row=row, column=0, padx=8, pady=6, sticky="w")
        entry = tk.Entry(root, width=52)
        entry.grid(row=row, column=1, padx=8, pady=6)
        fields[key] = entry

    now = datetime.now(timezone.utc).replace(microsecond=0)
    fields["not_before"].insert(0, now.isoformat())
    fields["expires_at"].insert(0, (now + timedelta(days=365)).isoformat())

    status = tk.Label(root, textvariable=status_var, fg="#B00020", justify="left", wraplength=460)
    status.grid(row=len(labels), column=0, columnspan=2, padx=8, pady=(2, 6), sticky="w")

    def generate() -> None:
        try:
            value = create_license(
                customer_name=fields["customer"].get(),
                device_code=fields["device"].get(),
                license_id=fields["license_id"].get(),
                not_before=fields["not_before"].get(),
                expires_at=fields["expires_at"].get(),
            )
            path = filedialog.asksaveasfilename(defaultextension=".license", filetypes=(("许可证文件", "*.license"),))
            if path:
                write_license(value, Path(path))
                messagebox.showinfo("完成", f"许可证已生成：{path}")
                status_var.set("许可证已生成。")
        except Exception as exc:
            status_var.set(f"无法生成许可证：{exc}")
            messagebox.showerror("无法生成许可证", str(exc))

    tk.Button(root, text="生成许可证", command=generate).grid(row=len(labels) + 1, column=1, padx=8, pady=12, sticky="e")
    root.mainloop()


if __name__ == "__main__":
    main()
