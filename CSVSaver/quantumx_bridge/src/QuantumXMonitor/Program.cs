using System;
using System.Configuration;
using System.Net;
using System.Windows.Forms;

namespace QuantumXMonitor
{
    internal static class Program
    {
        [STAThread]
        private static void Main(string[] args)
        {
            bool serverOnly = Array.Exists(
                args,
                argument => string.Equals(argument, "--server-only", StringComparison.OrdinalIgnoreCase));
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            string deviceIp = ConfigurationManager.AppSettings["QuantumXDeviceIp"];
            if (string.IsNullOrWhiteSpace(deviceIp))
            {
                deviceIp = "192.168.10.20";
            }
            IPAddress address;
            if (!IPAddress.TryParse(deviceIp, out address) ||
                address.AddressFamily != System.Net.Sockets.AddressFamily.InterNetwork)
            {
                throw new ConfigurationErrorsException("QuantumXDeviceIp must be a valid IPv4 address.");
            }
            Application.Run(new MonitorForm(serverOnly, deviceIp));
        }
    }
}
