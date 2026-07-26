using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;

internal static class LauncherShell
{
    [STAThread]
    private static void Main()
    {
        string root = AppDomain.CurrentDomain.BaseDirectory;
        string host = Path.Combine(root, "runtime", "pythonw.exe");
        if (!File.Exists(host)) host = Path.Combine(root, "runtime", "python.exe");
        string script = Path.Combine(root, "desktop_host.py");
        if (!File.Exists(host) || !File.Exists(script))
        {
            MessageBox.Show("软件文件不完整，请重新安装。\n错误编号：START-002", "热点图文批量生产工作台", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return;
        }

        ProcessStartInfo startInfo = new ProcessStartInfo(host, "\"" + script + "\"")
        {
            WorkingDirectory = root,
            UseShellExecute = false,
            CreateNoWindow = true,
            WindowStyle = ProcessWindowStyle.Hidden,
        };
        try
        {
            Process process = Process.Start(startInfo);
            if (process == null) throw new InvalidOperationException();
            try
            {
                process.WaitForExit();
                if (process.ExitCode != 0)
                    MessageBox.Show("软件启动失败，请重新启动。\n错误编号：START-001", "热点图文批量生产工作台", MessageBoxButtons.OK, MessageBoxIcon.Error);
            }
            finally
            {
                process.Dispose();
            }
        }
        catch
        {
            MessageBox.Show("软件启动失败，请重新启动。\n错误编号：START-001", "热点图文批量生产工作台", MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
    }
}
