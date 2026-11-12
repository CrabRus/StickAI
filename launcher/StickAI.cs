// StickAI.exe: starts StickAI with the project's virtual environment and no console window.
// Built by launcher/build.py with the C# compiler that ships with Windows (.NET Framework 4).
using System;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Windows.Forms;

[assembly: AssemblyTitle("StickAI")]
[assembly: AssemblyProduct("StickAI")]
[assembly: AssemblyDescription("Desktop stickmen")]

static class Launcher
{
    [STAThread]
    static int Main(string[] args)
    {
        string dir = AppDomain.CurrentDomain.BaseDirectory;
        string python = Path.Combine(dir, ".venv", "Scripts", "pythonw.exe");
        string main = Path.Combine(dir, "main.py");
        if (!File.Exists(python) || !File.Exists(main))
        {
            MessageBox.Show(
                "StickAI.exe must sit in the StickAI folder, next to main.py and the .venv folder.\n\n" +
                "First run (in that folder):\n  python -m venv .venv\n  .venv\\Scripts\\pip install -r requirements.txt",
                "StickAI", MessageBoxButtons.OK, MessageBoxIcon.Information);
            return 1;
        }
        string extra = string.Join(" ", args.Select(a => "\"" + a.Replace("\"", "\\\"") + "\""));
        var psi = new ProcessStartInfo(python, "\"" + main + "\" " + extra)
        {
            WorkingDirectory = dir,
            UseShellExecute = false,
            CreateNoWindow = true,
        };
        try
        {
            Process.Start(psi);
            return 0;
        }
        catch (Exception e)
        {
            MessageBox.Show("Can't start StickAI:\n" + e.Message, "StickAI", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }
}
