// 특정 프로그램(프로세스)의 소리만 녹음해 표준 출력으로 내보내는 작은 도구.
//
// Windows 10 2004(빌드 19041) 이후의 '프로세스 루프백' 기능을 씁니다. 가상 오디오 드라이버가 필요 없고,
// 스피커 볼륨·음소거를 적용하기 전의 소리를 받으므로 스피커를 꺼 두어도 녹음됩니다.
//
//   proc_capture.exe capture <프로세스 ID>   그 프로세스와 자식 프로세스의 소리만
//   proc_capture.exe except <프로세스 ID>    그 프로세스(와 자식)만 빼고 PC의 모든 소리
//       출력: 48000Hz · 2채널 · float32 (리틀 엔디언) 원시 데이터를 끊임없이 표준 출력으로
//
// 만들기 (Windows 에 기본으로 있는 C# 컴파일러, 외부 라이브러리 없음):
//   C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe /nologo /optimize /out:proc_capture.exe proc_capture.cs
using System;
using System.IO;
using System.Runtime.InteropServices;
using System.Threading;

static class ProcCapture
{
    const int RATE = 48000, CHANNELS = 2;

    [StructLayout(LayoutKind.Sequential)]
    struct WAVEFORMATEX { public ushort wFormatTag, nChannels; public uint nSamplesPerSec, nAvgBytesPerSec; public ushort nBlockAlign, wBitsPerSample, cbSize; }

    [StructLayout(LayoutKind.Sequential)]
    struct ACTIVATION_PARAMS { public int ActivationType; public uint TargetProcessId; public int ProcessLoopbackMode; }

    [StructLayout(LayoutKind.Sequential)]
    struct PROPVARIANT_BLOB { public ushort vt, r1, r2, r3; public uint cbSize; public IntPtr pBlobData; }

    [ComImport, Guid("1CB9AD4C-DBFA-4c32-B178-C2F568A703B2"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IAudioClient
    {
        [PreserveSig] int Initialize(int shareMode, uint flags, long bufferDuration, long periodicity, ref WAVEFORMATEX format, IntPtr session);
        [PreserveSig] int GetBufferSize(out uint frames);
        [PreserveSig] int GetStreamLatency(out long latency);
        [PreserveSig] int GetCurrentPadding(out uint padding);
        [PreserveSig] int IsFormatSupported(int shareMode, IntPtr format, IntPtr closest);
        [PreserveSig] int GetMixFormat(out IntPtr format);
        [PreserveSig] int GetDevicePeriod(out long def, out long min);
        [PreserveSig] int Start();
        [PreserveSig] int Stop();
        [PreserveSig] int Reset();
        [PreserveSig] int SetEventHandle(IntPtr handle);
        [PreserveSig] int GetService(ref Guid iid, [MarshalAs(UnmanagedType.IUnknown)] out object service);
    }

    [ComImport, Guid("C8ADBD64-E71E-48a0-A4DE-185C395CD317"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IAudioCaptureClient
    {
        [PreserveSig] int GetBuffer(out IntPtr data, out uint frames, out uint flags, out ulong devicePosition, out ulong qpcPosition);
        [PreserveSig] int ReleaseBuffer(uint frames);
        [PreserveSig] int GetNextPacketSize(out uint frames);
    }

    [ComImport, Guid("72A22D78-CDE4-431D-B8CC-843A71199B6D"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IActivateAudioInterfaceAsyncOperation
    {
        [PreserveSig] int GetActivateResult(out int hr, [MarshalAs(UnmanagedType.IUnknown)] out object iface);
    }

    [ComImport, Guid("41D949AB-9862-444A-80F6-C261334DA5EB"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IActivateAudioInterfaceCompletionHandler
    {
        void ActivateCompleted(IActivateAudioInterfaceAsyncOperation op);
    }

    // 완료 알림은 다른 스레드에서 오므로 '어느 스레드에서 불러도 되는(agile)' 객체여야 합니다.
    [ComImport, Guid("94ea2b94-e9cc-49e0-c0ff-ee64ca8f5b90"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IAgileObject { }

    [ClassInterface(ClassInterfaceType.None), ComVisible(true)]
    class Handler : IActivateAudioInterfaceCompletionHandler, IAgileObject
    {
        public readonly ManualResetEvent Done = new ManualResetEvent(false);
        public int Hr = -1;
        public object Client;
        public void ActivateCompleted(IActivateAudioInterfaceAsyncOperation op)
        {
            op.GetActivateResult(out Hr, out Client);
            Done.Set();
        }
    }

    [DllImport("Mmdevapi.dll", ExactSpelling = true, PreserveSig = false)]
    static extern void ActivateAudioInterfaceAsync([MarshalAs(UnmanagedType.LPWStr)] string path, ref Guid iid,
        IntPtr activationParams, IActivateAudioInterfaceCompletionHandler handler, out IActivateAudioInterfaceAsyncOperation op);

    static int Main(string[] args)
    {
        uint pid;
        if (args.Length < 2 || (args[0] != "capture" && args[0] != "except") || !uint.TryParse(args[1], out pid))
        { Console.Error.WriteLine("사용법: proc_capture.exe capture|except <프로세스 ID>"); return 2; }

        // 1=프로세스 루프백 / 모드 0=그 프로세스(와 자식)만, 1=그 프로세스(와 자식)만 빼고 전부
        var ap = new ACTIVATION_PARAMS { ActivationType = 1, TargetProcessId = pid, ProcessLoopbackMode = args[0] == "except" ? 1 : 0 };
        IntPtr apPtr = Marshal.AllocHGlobal(Marshal.SizeOf(ap));
        Marshal.StructureToPtr(ap, apPtr, false);
        var pv = new PROPVARIANT_BLOB { vt = 65, cbSize = (uint)Marshal.SizeOf(ap), pBlobData = apPtr };          // VT_BLOB
        IntPtr pvPtr = Marshal.AllocHGlobal(Marshal.SizeOf(pv));
        Marshal.StructureToPtr(pv, pvPtr, false);

        var handler = new Handler();
        Guid iidClient = typeof(IAudioClient).GUID;
        IActivateAudioInterfaceAsyncOperation op;
        try { ActivateAudioInterfaceAsync("VAD\\Process_Loopback", ref iidClient, pvPtr, handler, out op); }
        catch (Exception e) { Console.Error.WriteLine("ERROR activate " + e.Message); return 3; }
        if (!handler.Done.WaitOne(5000) || handler.Hr != 0 || handler.Client == null)
        { Console.Error.WriteLine("ERROR activate-result 0x" + handler.Hr.ToString("X8")); return 3; }

        var client = (IAudioClient)handler.Client;
        var fmt = new WAVEFORMATEX { wFormatTag = 3, nChannels = CHANNELS, nSamplesPerSec = RATE, wBitsPerSample = 32,
                                     nBlockAlign = CHANNELS * 4, nAvgBytesPerSec = RATE * CHANNELS * 4, cbSize = 0 };
        const uint LOOPBACK = 0x00020000, AUTOCONVERT = 0x80000000, SRC_QUALITY = 0x08000000;
        int hr = client.Initialize(0, LOOPBACK | AUTOCONVERT | SRC_QUALITY, 2000000, 0, ref fmt, IntPtr.Zero);   // 200ms 버퍼
        if (hr != 0) { Console.Error.WriteLine("ERROR initialize 0x" + hr.ToString("X8")); return 4; }
        Guid iidCap = typeof(IAudioCaptureClient).GUID;
        object capObj;
        hr = client.GetService(ref iidCap, out capObj);
        if (hr != 0) { Console.Error.WriteLine("ERROR service 0x" + hr.ToString("X8")); return 4; }
        var cap = (IAudioCaptureClient)capObj;
        hr = client.Start();
        if (hr != 0) { Console.Error.WriteLine("ERROR start 0x" + hr.ToString("X8")); return 4; }
        Console.Error.WriteLine("READY");

        Stream stdout = Console.OpenStandardOutput();
        byte[] buf = new byte[RATE * CHANNELS * 4];
        byte[] silence = new byte[0];
        try
        {
            while (true)
            {
                uint next;
                if (cap.GetNextPacketSize(out next) != 0) break;
                if (next == 0) { Thread.Sleep(10); continue; }
                IntPtr data; uint frames, flags; ulong dp, qp;
                if (cap.GetBuffer(out data, out frames, out flags, out dp, out qp) != 0) break;
                int bytes = (int)frames * CHANNELS * 4;
                if (bytes > buf.Length) buf = new byte[bytes];
                if ((flags & 0x2) != 0) Array.Clear(buf, 0, bytes);        // 무음 표시
                else Marshal.Copy(data, buf, 0, bytes);
                cap.ReleaseBuffer(frames);
                stdout.Write(buf, 0, bytes);
                stdout.Flush();
            }
        }
        catch (IOException) { }   // 받는 쪽이 끝나면 조용히 끝납니다
        client.Stop();
        return 0;
    }
}
