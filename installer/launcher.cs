// Barrens Voice.exe: what a player finds in the install folder, with the app's icon - it starts the
// launcher of the current version (app\current, a junction bv/update.py points at each version).
// Compiled by tools/installer.py with the C# compiler of the .NET Framework every Windows 10/11 has.
using System;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Text;
using System.Windows.Forms;

[assembly: DefaultDllImportSearchPaths(DllImportSearchPath.System32)]
static class BarrensVoice
{
    [STAThread]
    static int Main(string[] args)
    {
        if (args.Length == 2 && args[0] == "--check-cuda")
            return CheckCuda(args[1]);

        string root = AppDomain.CurrentDomain.BaseDirectory;
        // app\current - or, when a switch was cut short between its two renames (bv/install.py point_current:
        // current -> current.old, current.new -> current), the new version's link, then the old one's
        string current = new[] { "current", "current.new", "current.old" }
            .Select(name => Path.Combine(root, "app", name))
            .FirstOrDefault(Starts) ?? Path.Combine(root, "app", "current");
        string pythonw = Path.Combine(current, ".venv", "Scripts", "pythonw.exe");
        string script = Path.Combine(current, "companion", "BarrensVoice.pyw");
        if (!Starts(current))
        {
            MessageBox.Show("Barrens Voice установлен не до конца: нет " + (File.Exists(pythonw) ? script : pythonw) +
                            ".\n\nУстанови его заново.", "Barrens Voice", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
        var start = new ProcessStartInfo(pythonw)
        {
            Arguments = "\"" + script + "\"" + string.Concat(args.Select(a => " \"" + a.Replace("\"", "\\\"") + "\"")),
            WorkingDirectory = Path.Combine(current, "companion"),
            UseShellExecute = false,
        };
        Process.Start(start);
        return 0;
    }

    [DllImport("nvcuda.dll")]
    static extern int cuInit(uint flags);
    [DllImport("nvcuda.dll")]
    static extern int cuDriverGetVersion(out int version);
    [DllImport("nvcuda.dll")]
    static extern int cuDeviceGetCount(out int count);
    [DllImport("nvcuda.dll", CharSet = CharSet.Ansi, ExactSpelling = true)]
    static extern int cuDeviceGetName(StringBuilder name, int length, int device);

    static int CheckCuda(string resultPath)
    {
        try
        {
            int version, count;
            if (cuInit(0) != 0 || cuDeviceGetCount(out count) != 0 || count == 0 ||
                cuDriverGetVersion(out version) != 0 || version < 12000)
                return 2;
            var name = new StringBuilder(128);
            string[] cards = Enumerable.Range(0, count)
                .Select(i => cuDeviceGetName(name, name.Capacity, i) == 0 ? name.ToString() : "NVIDIA")
                .ToArray();
            File.WriteAllText(resultPath, "CUDA driver API " + version + "; " + String.Join(", ", cards));
            return 0;
        }
        catch (DllNotFoundException) { return 2; }
        catch (EntryPointNotFoundException) { return 2; }
        catch (BadImageFormatException) { return 2; }
    }

    // a version's folder the launcher can start from: its Python and the launcher's script are there - the same
    // files as bv/install.py STARTS, which the updater checks before it points app\current at a version
    static bool Starts(string version)
    {
        return File.Exists(Path.Combine(version, ".venv", "Scripts", "pythonw.exe")) &&
               File.Exists(Path.Combine(version, "companion", "BarrensVoice.pyw"));
    }
}
