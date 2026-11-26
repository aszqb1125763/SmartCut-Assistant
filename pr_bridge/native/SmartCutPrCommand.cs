using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;

internal static class SmartCutPrCommand
{
    private const uint WmCommand = 0x0111;
    private const uint MfByPosition = 0x0400;
    private const uint MfDisabled = 0x0002;
    private const uint MfGrayed = 0x0001;
    private const uint SmtoBlock = 0x0001;
    private const uint SmtoAbortIfHung = 0x0002;
    private const uint Th32csSnapProcess = 0x00000002;

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Auto)]
    private struct ProcessEntry32
    {
        public uint dwSize;
        public uint cntUsage;
        public uint th32ProcessID;
        public IntPtr th32DefaultHeapID;
        public uint th32ModuleID;
        public uint cntThreads;
        public uint th32ParentProcessID;
        public int pcPriClassBase;
        public uint dwFlags;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 260)]
        public string szExeFile;
    }

    private sealed class MenuCommand
    {
        public uint Id;
        public string Text;
        public string Path;
        public bool Enabled;
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern IntPtr CreateToolhelp32Snapshot(uint flags, uint processId);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool Process32First(IntPtr snapshot, ref ProcessEntry32 entry);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool Process32Next(IntPtr snapshot, ref ProcessEntry32 entry);

    [DllImport("kernel32.dll")]
    private static extern bool CloseHandle(IntPtr handle);

    [DllImport("user32.dll")]
    private static extern IntPtr GetMenu(IntPtr window);

    [DllImport("user32.dll")]
    private static extern int GetMenuItemCount(IntPtr menu);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern int GetMenuString(
        IntPtr menu, uint item, StringBuilder text, int maxCount, uint flags);

    [DllImport("user32.dll")]
    private static extern IntPtr GetSubMenu(IntPtr menu, int position);

    [DllImport("user32.dll")]
    private static extern uint GetMenuItemID(IntPtr menu, int position);

    [DllImport("user32.dll")]
    private static extern uint GetMenuState(IntPtr menu, uint item, uint flags);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern IntPtr SendMessageTimeout(
        IntPtr window, uint message, IntPtr wParam, IntPtr lParam,
        uint flags, uint timeoutMilliseconds, out IntPtr result);

    private static int Main(string[] args)
    {
        Console.OutputEncoding = new UTF8Encoding(false);
        try
        {
            if (args.Length < 2)
            {
                return Fail("用法：probe <CEP进程ID> 或 execute <extract|undo> <CEP进程ID>");
            }

            string mode = (args[0] ?? "").Trim().ToLowerInvariant();
            string commandName = null;
            int pidIndex = 1;
            if (mode == "execute")
            {
                if (args.Length < 3)
                {
                    return Fail("execute 缺少命令或 CEP 进程ID");
                }
                commandName = (args[1] ?? "").Trim().ToLowerInvariant();
                if (commandName != "extract" && commandName != "undo")
                {
                    return Fail("拒绝未授权命令：" + commandName);
                }
                pidIndex = 2;
            }
            else if (mode != "probe")
            {
                return Fail("不支持的模式：" + mode);
            }

            int cepProcessId;
            if (!Int32.TryParse(args[pidIndex], out cepProcessId) || cepProcessId <= 0)
            {
                return Fail("CEP 进程ID无效");
            }

            Process premiere = FindPremiereProcess(cepProcessId);
            if (premiere == null || premiere.MainWindowHandle == IntPtr.Zero)
            {
                return Fail("找不到当前 CEP 所属的 Premiere 主窗口");
            }

            IntPtr rootMenu = GetMenu(premiere.MainWindowHandle);
            if (rootMenu == IntPtr.Zero)
            {
                return Fail("Premiere 没有暴露原生菜单句柄");
            }

            MenuCommand extract = FindCommand(rootMenu, "extract");
            MenuCommand undo = FindCommand(rootMenu, "undo");
            string version = SafeVersion(premiere);

            if (mode == "probe")
            {
                Console.WriteLine(
                    "OK|probe|pid=" + premiere.Id +
                    "|hwnd=" + premiere.MainWindowHandle.ToInt64() +
                    "|version=" + Escape(version) +
                    "|extract=" + FormatCommand(extract) +
                    "|undo=" + FormatCommand(undo));
                return 0;
            }

            MenuCommand selected = commandName == "extract" ? extract : undo;
            if (selected == null || selected.Id == 0 || selected.Id == UInt32.MaxValue)
            {
                return Fail("没有找到 Premiere 原生命令：" + commandName);
            }

            // PR 在脚本刚设置入/出点后，HMENU 的 enabled 状态可能晚几十毫秒刷新。
            // 短暂重读真实菜单；若仍标记 disabled 也发送白名单命令，最终是否生效由
            // ExtendScript 回读序列长度判定，不能把缓存的菜单状态当成成功依据。
            for (int retry = 0; retry < 6 && !selected.Enabled; retry++)
            {
                Thread.Sleep(50);
                selected = FindCommand(GetMenu(premiere.MainWindowHandle), commandName);
                if (selected == null) break;
            }
            if (selected == null || selected.Id == 0 || selected.Id == UInt32.MaxValue)
            {
                return Fail("刷新菜单后命令消失：" + commandName);
            }

            IntPtr commandResult;
            IntPtr sent = SendMessageTimeout(
                premiere.MainWindowHandle,
                WmCommand,
                new IntPtr((long)selected.Id),
                IntPtr.Zero,
                SmtoBlock | SmtoAbortIfHung,
                5000,
                out commandResult);
            if (sent == IntPtr.Zero)
            {
                int error = Marshal.GetLastWin32Error();
                return Fail("发送原生命令失败或超时，Win32=" + error);
            }

            Console.WriteLine(
                "OK|execute|command=" + commandName +
                "|id=" + selected.Id +
                "|text=" + Escape(selected.Text) +
                "|menuEnabled=" + (selected.Enabled ? "1" : "0") +
                "|pid=" + premiere.Id +
                "|version=" + Escape(version));
            return 0;
        }
        catch (Exception error)
        {
            return Fail(error.GetType().Name + "：" + error.Message);
        }
    }

    private static Process FindPremiereProcess(int childPid)
    {
        Dictionary<int, int> parents = SnapshotParents();
        int current = childPid;
        for (int depth = 0; depth < 24 && current > 0; depth++)
        {
            Process candidate = SafeGetProcess(current);
            if (IsPremiere(candidate))
            {
                candidate.Refresh();
                return candidate;
            }
            int parent;
            if (!parents.TryGetValue(current, out parent) || parent <= 0 || parent == current)
            {
                break;
            }
            current = parent;
        }

        // 某些 CEP 构建会把渲染进程挂到独立 broker。只有系统中恰好一个可见
        // Premiere 主窗口时才允许回退，避免同时打开多个 PR 时发错项目。
        List<Process> visible = new List<Process>();
        foreach (Process process in Process.GetProcesses())
        {
            if (!IsPremiere(process))
            {
                process.Dispose();
                continue;
            }
            process.Refresh();
            if (process.MainWindowHandle != IntPtr.Zero)
            {
                visible.Add(process);
            }
            else
            {
                process.Dispose();
            }
        }
        return visible.Count == 1 ? visible[0] : null;
    }

    private static bool IsPremiere(Process process)
    {
        if (process == null) return false;
        string name;
        try { name = process.ProcessName; }
        catch { return false; }
        return String.Equals(name, "Adobe Premiere Pro", StringComparison.OrdinalIgnoreCase);
    }

    private static Process SafeGetProcess(int pid)
    {
        try { return Process.GetProcessById(pid); }
        catch { return null; }
    }

    private static Dictionary<int, int> SnapshotParents()
    {
        Dictionary<int, int> result = new Dictionary<int, int>();
        IntPtr snapshot = CreateToolhelp32Snapshot(Th32csSnapProcess, 0);
        if (snapshot == new IntPtr(-1)) return result;
        try
        {
            ProcessEntry32 entry = new ProcessEntry32();
            entry.dwSize = (uint)Marshal.SizeOf(typeof(ProcessEntry32));
            if (!Process32First(snapshot, ref entry)) return result;
            do
            {
                result[(int)entry.th32ProcessID] = (int)entry.th32ParentProcessID;
                entry.dwSize = (uint)Marshal.SizeOf(typeof(ProcessEntry32));
            }
            while (Process32Next(snapshot, ref entry));
        }
        finally
        {
            CloseHandle(snapshot);
        }
        return result;
    }

    private static MenuCommand FindCommand(IntPtr rootMenu, string commandName)
    {
        List<MenuCommand> commands = new List<MenuCommand>();
        WalkMenu(rootMenu, "", commands);
        foreach (MenuCommand command in commands)
        {
            string normalized = Normalize(command.Text);
            if (commandName == "extract")
            {
                if (normalized == "提取" ||
                    normalized.Equals("Extract", StringComparison.OrdinalIgnoreCase))
                {
                    return command;
                }
            }
            else if (commandName == "undo")
            {
                if (normalized.StartsWith("撤销", StringComparison.Ordinal) ||
                    normalized.StartsWith("撤消", StringComparison.Ordinal) ||
                    normalized.StartsWith("Undo", StringComparison.OrdinalIgnoreCase))
                {
                    return command;
                }
            }
        }
        return null;
    }

    private static void WalkMenu(IntPtr menu, string parentPath, List<MenuCommand> output)
    {
        int count = GetMenuItemCount(menu);
        for (int position = 0; position < count; position++)
        {
            StringBuilder text = new StringBuilder(512);
            GetMenuString(menu, (uint)position, text, text.Capacity, MfByPosition);
            IntPtr subMenu = GetSubMenu(menu, position);
            uint id = GetMenuItemID(menu, position);
            string label = text.ToString();
            string path = parentPath + "/" + label;
            if (subMenu != IntPtr.Zero)
            {
                WalkMenu(subMenu, path, output);
                continue;
            }
            if (id == 0 || id == UInt32.MaxValue) continue;
            uint state = GetMenuState(menu, (uint)position, MfByPosition);
            output.Add(new MenuCommand
            {
                Id = id,
                Text = label,
                Path = path,
                Enabled = (state & (MfDisabled | MfGrayed)) == 0
            });
        }
    }

    private static string Normalize(string value)
    {
        string result = value ?? "";
        int tab = result.IndexOf('\t');
        if (tab >= 0) result = result.Substring(0, tab);
        result = Regex.Replace(result, @"\(&.\)", "");
        result = result.Replace("&", "").Trim();
        return result;
    }

    private static string SafeVersion(Process process)
    {
        try { return process.MainModule.FileVersionInfo.FileVersion ?? ""; }
        catch { return ""; }
    }

    private static string FormatCommand(MenuCommand command)
    {
        if (command == null) return "missing";
        return command.Id + "," + (command.Enabled ? "enabled" : "disabled") +
               "," + Escape(command.Text);
    }

    private static string Escape(string value)
    {
        return (value ?? "").Replace("|", "/").Replace("\r", " ").Replace("\n", " ");
    }

    private static int Fail(string message)
    {
        Console.Error.WriteLine("ERR|" + Escape(message));
        return 2;
    }
}
