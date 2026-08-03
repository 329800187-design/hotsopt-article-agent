using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Text;
using System.Text.RegularExpressions;
using System.Windows.Forms;
using Microsoft.Win32;

internal static class SetupBootstrapper
{
    private static readonly byte[] Marker = Encoding.UTF8.GetBytes("HOTSPOT_RC131_PAYLOAD\n");
    private const string ProductFolderName = "热点图文批量生产工作台";
    private const string DisplayName = "热点图文批量生产工作台";
    private const string Version = "RC1.3.3-Lite-R2.2.17";
    private const string Publisher = "热点图文工作台";
    private const string UninstallKeyName = "HotspotArticleAgent";

    private sealed class InstallOptions
    {
        public string InstallRoot { get; set; }
        public bool CreateDesktopShortcut { get; set; }
        public bool CreateStartMenuShortcut { get; set; }
        public bool RunAfterInstall { get; set; }

        public InstallOptions()
        {
            InstallRoot = DefaultInstallRoot();
            CreateDesktopShortcut = true;
            CreateStartMenuShortcut = true;
            RunAfterInstall = true;
        }
    }

    [STAThread]
    private static void Main(string[] args)
    {
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        string defaultInstallRoot = DefaultInstallRoot();
        try
        {
            if (args.Any(arg => string.Equals(arg, "--uninstall", StringComparison.OrdinalIgnoreCase)))
            {
                string installedRoot = AppDomain.CurrentDomain.BaseDirectory.TrimEnd(Path.DirectorySeparatorChar);
                if (!File.Exists(Path.Combine(installedRoot, "热点图文工作台.exe")))
                    installedRoot = defaultInstallRoot;
                Uninstall(installedRoot);
                return;
            }
            int cleanupIndex = Array.FindIndex(args, arg => string.Equals(arg, "--cleanup", StringComparison.OrdinalIgnoreCase));
            if (cleanupIndex >= 0)
            {
                string cleanupRoot = cleanupIndex + 1 < args.Length ? args[cleanupIndex + 1] : defaultInstallRoot;
                bool keepData = !args.Any(arg => string.Equals(arg, "--delete-data", StringComparison.OrdinalIgnoreCase));
                int parentPid = 0;
                int.TryParse(cleanupIndex + 2 < args.Length ? args[cleanupIndex + 2] : "0", out parentPid);
                CleanupAfterUninstall(cleanupRoot, keepData, parentPid);
                return;
            }

            bool silent = args.Any(arg => string.Equals(arg, "--silent", StringComparison.OrdinalIgnoreCase));
            InstallOptions options = silent ? new InstallOptions() : ShowWizard();
            if (options == null) return;
            Install(options);
            if (!silent)
                MessageBox.Show("安装完成。您可以从桌面图标或开始菜单打开软件。", DisplayName, MessageBoxButtons.OK, MessageBoxIcon.Information);
            if (options.RunAfterInstall)
                Process.Start(new ProcessStartInfo(Path.Combine(options.InstallRoot, "热点图文工作台.exe")) { UseShellExecute = true });
        }
        catch (Exception error)
        {
            string message = error.Message.IndexOf("WEBVIEW2", StringComparison.OrdinalIgnoreCase) >= 0
                ? "当前电脑缺少 WebView2 运行环境，请联网后重新安装。\n错误编号：WEBVIEW2-001"
                : "安装失败，请重新下载后再试。\n错误编号：SETUP-001";
            MessageBox.Show(message, DisplayName, MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
    }

    private static string DefaultInstallRoot()
    {
        return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Programs", ProductFolderName);
    }

    private static InstallOptions ShowWizard()
    {
        InstallOptions options = new InstallOptions();
        using (Form form = new Form())
        {
        form.Text = DisplayName + " 安装向导";
        form.StartPosition = FormStartPosition.CenterScreen;
        form.FormBorderStyle = FormBorderStyle.FixedDialog;
        form.MaximizeBox = false;
        form.MinimizeBox = false;
        form.ClientSize = new Size(620, 360);
        form.Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath);

        Label title = new Label { Text = DisplayName, Font = new Font("Microsoft YaHei UI", 15, FontStyle.Bold), AutoSize = true, Left = 28, Top = 24 };
        Label version = new Label { Text = "版本：" + Version, AutoSize = true, Left = 30, Top = 64 };
        Label location = new Label { Text = "安装位置：", AutoSize = true, Left = 30, Top = 104 };
        TextBox pathBox = new TextBox { Left = 30, Top = 128, Width = 450, Text = options.InstallRoot };
        Button browse = new Button { Left = 492, Top = 126, Width = 98, Height = 28, Text = "更改安装位置" };
        CheckBox desktop = new CheckBox { Left = 32, Top = 174, Width = 260, Checked = true, Text = "创建桌面快捷方式" };
        CheckBox startMenu = new CheckBox { Left = 32, Top = 204, Width = 260, Checked = true, Text = "创建开始菜单快捷方式" };
        CheckBox runAfter = new CheckBox { Left = 32, Top = 234, Width = 260, Checked = true, Text = "安装完成后运行软件" };
        Label hint = new Label { Left = 30, Top = 274, Width = 560, Height = 34, Text = "建议安装到当前用户目录或其他有写入权限的位置，普通用户无需管理员权限。" };
        Button cancel = new Button { Left = 400, Top = 318, Width = 86, Height = 30, Text = "取消", DialogResult = DialogResult.Cancel };
        Button install = new Button { Left = 504, Top = 318, Width = 86, Height = 30, Text = "开始安装", DialogResult = DialogResult.OK };

        browse.Click += (sender, eventArgs) =>
        {
            using (FolderBrowserDialog dialog = new FolderBrowserDialog())
            {
            dialog.Description = "请选择安装位置";
            dialog.SelectedPath = Directory.Exists(pathBox.Text) ? pathBox.Text : Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
            if (dialog.ShowDialog(form) == DialogResult.OK)
                pathBox.Text = Path.Combine(dialog.SelectedPath, ProductFolderName);
            }
        };
        form.Controls.AddRange(new Control[] { title, version, location, pathBox, browse, desktop, startMenu, runAfter, hint, cancel, install });
        form.AcceptButton = install;
        form.CancelButton = cancel;
        if (form.ShowDialog() != DialogResult.OK) return null;
        options.InstallRoot = Path.GetFullPath(pathBox.Text.Trim());
        options.CreateDesktopShortcut = desktop.Checked;
        options.CreateStartMenuShortcut = startMenu.Checked;
        options.RunAfterInstall = runAfter.Checked;
        return options;
        }
    }

    private static void Install(InstallOptions options)
    {
        string installRoot = options.InstallRoot;
        string previousRoot = PreviousInstallRoot();
        if (!string.IsNullOrWhiteSpace(previousRoot) && Directory.Exists(previousRoot))
        {
            DialogResult upgrade = MessageBox.Show(
                "检测到旧版本，升级将保留文章、模型配置和激活信息。\n\n安装程序会先关闭旧版后台进程，再替换程序文件。",
                DisplayName,
                MessageBoxButtons.OKCancel,
                MessageBoxIcon.Information);
            if (upgrade == DialogResult.Cancel) return;
            StopProductProcesses(previousRoot, Process.GetCurrentProcess().Id);
            CleanProgramFiles(previousRoot);
        }
        StopProductProcesses(installRoot, Process.GetCurrentProcess().Id);
        Directory.CreateDirectory(installRoot);
        EnsureWritable(installRoot);
        string executable = Process.GetCurrentProcess().MainModule.FileName;
        byte[] payload = File.ReadAllBytes(executable);
        int marker = Find(payload, Marker);
        if (marker < 0) throw new InvalidDataException("payload missing");
        using (MemoryStream stream = new MemoryStream(payload, marker + Marker.Length, payload.Length - marker - Marker.Length, false))
        using (ZipArchive archive = new ZipArchive(stream, ZipArchiveMode.Read))
        {
            foreach (ZipArchiveEntry entry in archive.Entries)
            {
                string target = Path.GetFullPath(Path.Combine(installRoot, entry.FullName.Replace('/', Path.DirectorySeparatorChar)));
                if (!target.StartsWith(Path.GetFullPath(installRoot) + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase))
                    throw new InvalidDataException("unsafe archive path");
                if (string.IsNullOrEmpty(entry.Name)) { Directory.CreateDirectory(target); continue; }
                Directory.CreateDirectory(Path.GetDirectoryName(target));
                entry.ExtractToFile(target, true);
            }
        }
        EnsureWebView2Runtime(installRoot);
        string launcher = Path.Combine(installRoot, "热点图文工作台.exe");
        string uninstaller = Path.Combine(installRoot, "unins000.exe");
        File.Copy(executable, uninstaller, true);
        if (options.CreateDesktopShortcut)
            CreateShortcut(DesktopShortcutPath(), launcher, "打开热点图文工作台");
        if (options.CreateStartMenuShortcut)
        {
            string startMenu = StartMenuFolder();
            Directory.CreateDirectory(startMenu);
            CreateShortcut(Path.Combine(startMenu, "热点图文工作台.lnk"), launcher, "打开热点图文工作台");
            CreateShortcut(Path.Combine(startMenu, "卸载热点图文工作台.lnk"), uninstaller, "卸载热点图文工作台", "--uninstall");
        }
        RegisterInstalledApp(installRoot, launcher, uninstaller);
        VerifyInstalledAppRegistration(installRoot);
    }

    private static string UserDataRoot()
    {
        return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), ProductFolderName);
    }

    private static string PreviousInstallRoot()
    {
        try
        {
            using (RegistryKey key = Registry.CurrentUser.OpenSubKey(@"Software\Microsoft\Windows\CurrentVersion\Uninstall\" + UninstallKeyName))
            {
                string value = key == null ? "" : Convert.ToString(key.GetValue("InstallLocation")) ?? "";
                return string.IsNullOrWhiteSpace(value) ? "" : Path.GetFullPath(value);
            }
        }
        catch
        {
            return "";
        }
    }

    private static void CleanProgramFiles(string installRoot)
    {
        string full = Path.GetFullPath(installRoot);
        if (!Directory.Exists(full) || full.Length < 10) return;
        foreach (string path in Directory.GetFileSystemEntries(full))
        {
            string name = Path.GetFileName(path);
            if (string.Equals(name, "data", StringComparison.OrdinalIgnoreCase) ||
                string.Equals(name, "export", StringComparison.OrdinalIgnoreCase) ||
                string.Equals(name, "logs", StringComparison.OrdinalIgnoreCase))
                continue;
            try
            {
                if (Directory.Exists(path)) Directory.Delete(path, true);
                else File.Delete(path);
            }
            catch { }
        }
    }

    private static void StopProductProcesses(string installRoot)
    {
        StopProductProcesses(installRoot, -1);
    }

    private static void StopProductProcesses(string installRoot, int excludePid)
    {
        string runtimeRoot = Path.Combine(UserDataRoot(), "runtime");
        foreach (string file in new[] { "desktop.lock", "api.json", "web.json", "api.pid", "web.pid" })
            TryKillPidFromMetadata(Path.Combine(runtimeRoot, file), installRoot, excludePid);
        try
        {
            foreach (Process process in Process.GetProcesses())
            {
                if (excludePid > 0 && process.Id == excludePid) continue;
                string path = "";
                try { path = process.MainModule == null ? "" : process.MainModule.FileName; } catch { }
                if (!string.IsNullOrWhiteSpace(path) && Path.GetFullPath(path).StartsWith(Path.GetFullPath(installRoot), StringComparison.OrdinalIgnoreCase))
                    TryKillProcess(process);
            }
        }
        catch { }
    }

    private static void TryKillPidFromMetadata(string metadataPath, string installRoot, int excludePid)
    {
        if (!File.Exists(metadataPath)) return;
        string text = "";
        try { text = File.ReadAllText(metadataPath, Encoding.UTF8); } catch { return; }
        foreach (Match match in Regex.Matches(text, @"""(?:pid|main_pid|api_pid|web_pid)""\s*:\s*(\d+)|^\s*(\d+)\s*$", RegexOptions.Multiline))
        {
            string value = match.Groups[1].Success ? match.Groups[1].Value : match.Groups[2].Value;
            int pid;
            if (!int.TryParse(value, out pid) || pid <= 0) continue;
            if (excludePid > 0 && pid == excludePid) continue;
            try
            {
                Process process = Process.GetProcessById(pid);
                string path = "";
                try { path = process.MainModule == null ? "" : process.MainModule.FileName; } catch { }
                string expectedRoot = Regex.Match(text, @"""install_path""\s*:\s*""([^""]+)""").Groups[1].Value.Replace(@"\\", @"\");
                bool belongsToProduct = (!string.IsNullOrWhiteSpace(path) && path.StartsWith(Path.GetFullPath(installRoot), StringComparison.OrdinalIgnoreCase)) ||
                    (!string.IsNullOrWhiteSpace(expectedRoot) && !string.IsNullOrWhiteSpace(path) && path.StartsWith(Path.GetFullPath(expectedRoot), StringComparison.OrdinalIgnoreCase));
                if (belongsToProduct) TryKillProcess(process);
            }
            catch { }
        }
        try { File.Delete(metadataPath); } catch { }
    }

    private static void TryKillProcess(Process process)
    {
        try
        {
            if (process.HasExited) return;
            process.Kill();
            process.WaitForExit(8000);
        }
        catch { }
    }

    private static void EnsureWritable(string installRoot)
    {
        string probe = Path.Combine(installRoot, ".write-test");
        File.WriteAllText(probe, "ok", Encoding.UTF8);
        File.Delete(probe);
    }

    private static string DesktopShortcutPath()
    {
        return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory), "热点图文工作台.lnk");
    }

    private static string StartMenuFolder()
    {
        return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.StartMenu), "Programs", ProductFolderName);
    }

    private static void RegisterInstalledApp(string installRoot, string launcher, string uninstaller)
    {
        using (RegistryKey key = Registry.CurrentUser.CreateSubKey(@"Software\Microsoft\Windows\CurrentVersion\Uninstall\" + UninstallKeyName))
        {
        key.SetValue("DisplayName", DisplayName);
        key.SetValue("DisplayVersion", Version);
        key.SetValue("Publisher", Publisher);
        key.SetValue("InstallLocation", installRoot);
        key.SetValue("DisplayIcon", launcher + ",0");
        key.SetValue("UninstallString", "\"" + uninstaller + "\" --uninstall");
        key.SetValue("QuietUninstallString", "\"" + uninstaller + "\" --uninstall --silent");
        key.SetValue("EstimatedSize", EstimateSizeKb(installRoot), RegistryValueKind.DWord);
        key.SetValue("InstallDate", DateTime.Now.ToString("yyyyMMdd"));
        key.SetValue("NoModify", 1, RegistryValueKind.DWord);
        key.SetValue("NoRepair", 1, RegistryValueKind.DWord);
        }
    }

    private static int EstimateSizeKb(string installRoot)
    {
        try
        {
            long bytes = Directory.GetFiles(installRoot, "*", SearchOption.AllDirectories).Sum(file => new FileInfo(file).Length);
            return Math.Max(1, (int)Math.Min(int.MaxValue, bytes / 1024));
        }
        catch
        {
            return 1;
        }
    }

    private static void VerifyInstalledAppRegistration(string installRoot)
    {
        using (RegistryKey key = Registry.CurrentUser.OpenSubKey(@"Software\Microsoft\Windows\CurrentVersion\Uninstall\" + UninstallKeyName))
        {
            if (key == null) throw new InvalidOperationException("SETUP_REGISTRY_MISSING");
            string displayVersion = Convert.ToString(key.GetValue("DisplayVersion")) ?? "";
            string location = Convert.ToString(key.GetValue("InstallLocation")) ?? "";
            string uninstall = Convert.ToString(key.GetValue("UninstallString")) ?? "";
            if (displayVersion != Version || string.IsNullOrWhiteSpace(location) || string.IsNullOrWhiteSpace(uninstall))
                throw new InvalidOperationException("SETUP_REGISTRY_VERIFY_FAILED");
            if (!Path.GetFullPath(location).Equals(Path.GetFullPath(installRoot), StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException("SETUP_REGISTRY_LOCATION_MISMATCH");
        }
    }

    private static void CreateShortcut(string path, string target, string description, string arguments = "")
    {
        Type shellType = Type.GetTypeFromProgID("WScript.Shell");
        if (shellType == null) throw new InvalidOperationException();
        dynamic shell = Activator.CreateInstance(shellType);
        dynamic shortcut = shell.CreateShortcut(path);
        shortcut.TargetPath = target;
        shortcut.Arguments = arguments;
        shortcut.WorkingDirectory = Path.GetDirectoryName(target);
        shortcut.IconLocation = target + ",0";
        shortcut.Description = description;
        shortcut.Save();
    }

    private static void EnsureWebView2Runtime(string installRoot)
    {
        if (IsWebView2RuntimeInstalled()) return;
        string bootstrapper = Path.Combine(installRoot, "webview2", "MicrosoftEdgeWebView2Setup.exe");
        if (!File.Exists(bootstrapper)) throw new InvalidOperationException("WEBVIEW2_BOOTSTRAPPER_MISSING");
        ProcessStartInfo startInfo = new ProcessStartInfo(bootstrapper, "/silent /install")
        {
            UseShellExecute = false,
            CreateNoWindow = true,
            WindowStyle = ProcessWindowStyle.Hidden,
        };
        using (Process process = Process.Start(startInfo))
        {
            if (process == null) throw new InvalidOperationException("WEBVIEW2_INSTALL_FAILED");
            process.WaitForExit();
        }
        if (!IsWebView2RuntimeInstalled()) throw new InvalidOperationException("WEBVIEW2_INSTALL_FAILED");
    }

    private static bool IsWebView2RuntimeInstalled()
    {
        const string clientId = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}";
        string[] subkeys =
        {
            @"SOFTWARE\Microsoft\EdgeUpdate\Clients\" + clientId,
            @"SOFTWARE\Microsoft\EdgeUpdate\ClientState\" + clientId,
        };
        RegistryView[] views = { RegistryView.Registry64, RegistryView.Registry32 };
        RegistryHive[] hives = { RegistryHive.CurrentUser, RegistryHive.LocalMachine };
        foreach (RegistryHive hive in hives)
        foreach (RegistryView view in views)
        foreach (string subkey in subkeys)
        {
            try
            {
                using (RegistryKey key = RegistryKey.OpenBaseKey(hive, view).OpenSubKey(subkey))
                {
                string version = key == null ? "" : Convert.ToString(key.GetValue("pv")) ?? "";
                if (!string.IsNullOrWhiteSpace(version) && version != "0.0.0.0") return true;
                }
            }
            catch (Exception)
            {
            }
        }
        return false;
    }

    private static int Find(byte[] source, byte[] pattern)
    {
        for (int i = 0; i <= source.Length - pattern.Length; i++)
        {
            if (source.Skip(i).Take(pattern.Length).SequenceEqual(pattern)) return i;
        }
        return -1;
    }

    private static void Uninstall(string installRoot)
    {
        bool keepData = true;
        bool silent = Environment.GetCommandLineArgs().Any(arg => string.Equals(arg, "--silent", StringComparison.OrdinalIgnoreCase));
        if (!silent)
        {
            DialogResult result = MessageBox.Show(
                "卸载时是否保留历史文章、模型配置和激活信息？\n\n选择“是”：保留用户数据。\n选择“否”：完全删除用户数据。",
                DisplayName,
                MessageBoxButtons.YesNoCancel,
                MessageBoxIcon.Question);
            if (result == DialogResult.Cancel) return;
            keepData = result == DialogResult.Yes;
        }
        string cleaner = Path.Combine(Path.GetTempPath(), "热点图文工作台卸载清理_" + Guid.NewGuid().ToString("N") + ".exe");
        File.Copy(Application.ExecutablePath, cleaner, true);
        string args = "--cleanup \"" + installRoot + "\" " + Process.GetCurrentProcess().Id + (keepData ? "" : " --delete-data");
        Process.Start(new ProcessStartInfo(cleaner, args) { UseShellExecute = true, WindowStyle = ProcessWindowStyle.Normal });
        if (!silent)
            MessageBox.Show("卸载清理程序已启动，将在当前窗口关闭后删除安装目录。", DisplayName, MessageBoxButtons.OK, MessageBoxIcon.Information);
    }

    private static void CleanupAfterUninstall(string installRoot, bool keepData, int parentPid)
    {
        try
        {
            if (parentPid > 0)
            {
                try { Process.GetProcessById(parentPid).WaitForExit(20000); } catch { }
            }
            StopProductProcesses(installRoot, Process.GetCurrentProcess().Id);
            File.Delete(DesktopShortcutPath());
            string startMenu = StartMenuFolder();
            if (Directory.Exists(startMenu)) Directory.Delete(startMenu, true);
            Registry.CurrentUser.DeleteSubKeyTree(@"Software\Microsoft\Windows\CurrentVersion\Uninstall\" + UninstallKeyName, false);
            TryDeleteTree(installRoot);
            if (!keepData) TryDeleteTree(UserDataRoot());
            bool installGone = !Directory.Exists(installRoot);
            bool registryGone = Registry.CurrentUser.OpenSubKey(@"Software\Microsoft\Windows\CurrentVersion\Uninstall\" + UninstallKeyName) == null;
            if (installGone && registryGone)
                MessageBox.Show("卸载完成。", DisplayName, MessageBoxButtons.OK, MessageBoxIcon.Information);
            else
                MessageBox.Show("卸载未完全完成，请重启电脑后删除安装目录。\n错误编号：UNINSTALL-VERIFY-001", DisplayName, MessageBoxButtons.OK, MessageBoxIcon.Warning);
        }
        catch
        {
            MessageBox.Show("卸载失败，请重启电脑后再试。\n错误编号：UNINSTALL-001", DisplayName, MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
        try { File.Delete(Application.ExecutablePath); } catch { }
    }

    private static void TryDeleteTree(string target)
    {
        string full = Path.GetFullPath(target);
        if (string.IsNullOrWhiteSpace(full) || full.Length < 10) return;
        try
        {
            if (Directory.Exists(full)) Directory.Delete(full, true);
        }
        catch { }
    }
}
