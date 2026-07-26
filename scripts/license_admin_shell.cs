using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;

internal static class LicenseAdminShell
{
    [STAThread]
    private static void Main()
    {
        string root = AppDomain.CurrentDomain.BaseDirectory.TrimEnd('\\');
        string[] candidates =
        {
            Path.Combine(root, ".venv", "Scripts", "python.exe"),
            Path.Combine(root, "runtime", "python.exe"),
            "python.exe"
        };
        string python = null;
        foreach (string candidate in candidates)
        {
            if (candidate == "python.exe" || File.Exists(candidate))
            {
                python = candidate;
                break;
            }
        }
        if (python == null)
        {
            MessageBox.Show("\u672a\u627e\u5230\u53ef\u7528\u8fd0\u884c\u73af\u5883\uff0c\u8bf7\u4f7f\u7528\u9879\u76ee\u5b8c\u6574\u76ee\u5f55\u3002", "\u6388\u6743\u7b7e\u53d1\u5de5\u5177", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return;
        }
        try
        {
            Process.Start(new ProcessStartInfo
            {
                FileName = python,
                Arguments = "-m license_admin.license_generator_gui",
                WorkingDirectory = root,
                UseShellExecute = false,
                CreateNoWindow = true
            });
        }
        catch (Exception)
        {
            MessageBox.Show("\u6388\u6743\u7b7e\u53d1\u5de5\u5177\u65e0\u6cd5\u542f\u52a8\uff0c\u8bf7\u68c0\u67e5\u9879\u76ee\u6587\u4ef6\u662f\u5426\u5b8c\u6574\u3002", "\u6388\u6743\u7b7e\u53d1\u5de5\u5177", MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
    }
}
