from __future__ import annotations

import tkinter as tk
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tkinter import filedialog, messagebox

from license_admin.license_generator import create_license, write_license
from license_admin.signing_identity import signer_preflight


def main() -> None:
    preflight = signer_preflight()

    root = tk.Tk()
    root.title("离线许可证签发")
    fields: dict[str, tk.Entry] = {}
    status_var = tk.StringVar(value="")

    if not preflight["ready"]:
        status_var.set(
            f"错误编号：{preflight['code']}\n"
            f"{preflight['message']}\n"
            f"私钥查找路径：{preflight['private_key_path']}"
        )
    else:
        status_var.set("LICENSE_SIGNER_READY：签发身份预检通过。")

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

    def copy_diagnostic() -> None:
        diagnostic = "\n".join(
            (
                f"错误码={preflight['code']}",
                f"私钥查找路径={preflight['private_key_path']}",
                f"私钥是否存在={preflight['private_key_exists']}",
                f"公钥路径={preflight['public_key_path']}",
                f"公私钥是否匹配={preflight['keypair_matches']}",
                f"应用版本={preflight['app_version']}",
                f"构建提交={preflight['build_commit']}",
            )
        )
        root.clipboard_clear()
        root.clipboard_append(diagnostic)
        status_var.set("签发诊断已复制，不包含私钥内容。")

    tk.Button(root, text="复制签发诊断", command=copy_diagnostic).grid(row=len(labels) + 1, column=0, padx=8, pady=12, sticky="w")
    tk.Button(
        root,
        text="生成许可证",
        command=generate,
        state=tk.NORMAL if preflight["ready"] else tk.DISABLED,
    ).grid(row=len(labels) + 1, column=1, padx=8, pady=12, sticky="e")
    root.mainloop()


if __name__ == "__main__":
    main()
