// Standalone .NET Framework 4.8 service; no installer or configurable command API.
// Contracts: https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects
// https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-createprocessw
// https://learn.microsoft.com/en-us/dotnet/api/system.serviceprocess.servicebase.onstart
using System;
using System.IO;
using System.Text;
using System.Text.RegularExpressions;
using System.Collections.Generic;
using System.Diagnostics;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Security.Cryptography;
using System.Runtime.InteropServices;
using System.Threading;
#if !PORTABLE_FIXTURE
using System.ServiceProcess;
#endif

namespace EnforcedServiceV1 {
 public sealed class Settings {
  public readonly Dictionary<string,string> Values;
  static readonly string[] Keys={"format","service_name","service_sid","python_path","python_sha256","launcher_path","launcher_sha256","plan_path","plan_sha256","approval_path","approval_sha256","mode","host_sha256"};
  public Settings(string text) {
   if(text==null||text.Length>16384||text.IndexOf('\0')>=0||text.IndexOf('\r')>=0)throw new InvalidOperationException();
   string[] lines=text.Split('\n');if(lines.Length!=Keys.Length+1||lines[Keys.Length]!="")throw new InvalidOperationException();
   Values=new Dictionary<string,string>(StringComparer.Ordinal);
   for(int i=0;i<Keys.Length;i++){string prefix=Keys[i]+"=";if(!lines[i].StartsWith(prefix,StringComparison.Ordinal))throw new InvalidOperationException();string v=lines[i].Substring(prefix.Length);if(v.Length==0)throw new InvalidOperationException();Values.Add(Keys[i],v);}
   if(Values["format"]!="enforced-service-v1"||!Regex.IsMatch(Values["service_name"],"\\A[A-Za-z0-9_-]{1,80}\\z")||!Regex.IsMatch(Values["service_sid"],"\\AS-1-5-80-(?:[0-9]{1,10}-){4}[0-9]{1,10}\\z")|| (Values["mode"]!="initialize"&&Values["mode"]!="advance"&&Values["mode"]!="serve"))throw new InvalidOperationException();
   foreach(string k in new[]{"python_sha256","launcher_sha256","plan_sha256","approval_sha256","host_sha256"})if(!Regex.IsMatch(Values[k],"\\A[0-9a-f]{64}\\z"))throw new InvalidOperationException();
   foreach(string k in new[]{"python_path","launcher_path","plan_path","approval_path"})CheckPath(Values[k]);
  }
  public static void CheckPath(string p){if(p==null||!Regex.IsMatch(p,"\\A[A-Za-z]:\\\\")||p.IndexOf('"')>=0||p.IndexOf('\0')>=0||p.IndexOf('\r')>=0||p.IndexOf('\n')>=0||p.Substring(2).IndexOf(':')>=0||p.EndsWith("\\",StringComparison.Ordinal))throw new InvalidOperationException();foreach(string s in p.Substring(3).Split('\\'))if(s==""||s=="."||s==".."||s.EndsWith(".",StringComparison.Ordinal)||s.EndsWith(" ",StringComparison.Ordinal))throw new InvalidOperationException();}
  static string Quote(string p){CheckPath(p);return "\""+p+"\"";}
  public string CommandLine(){return Quote(Values["python_path"])+" -I -B "+Quote(Values["launcher_path"])+" "+Values["mode"]+" --plan "+Quote(Values["plan_path"])+" --approval "+Quote(Values["approval_path"])+" --journal "+Quote(Values["plan_path"].Substring(0,Values["plan_path"].LastIndexOf('\\')+1)+"journal\\controller-state.json")+(Values["mode"]=="serve"?" --arm":"");}
  public static bool IsTrustedOsRootOwner(string owner,string path,string osRoot,int depth){
   const string sid="S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464";
   return owner==sid&&depth>1&&path!=null&&osRoot!=null&&path.Length==3&&osRoot.Length==3&&
    ((path[0]>='A'&&path[0]<='Z')||(path[0]>='a'&&path[0]<='z'))&&path[1]==':'&&path[2]=='\\'&&
    String.Equals(path,osRoot,StringComparison.OrdinalIgnoreCase);
  }
  public static int ServiceExitCode(string mode,uint childCode){return (mode=="initialize"||mode=="advance")&&childCode==0?0:1;}
 }
#if !PORTABLE_FIXTURE
 public static class Protection {
  static readonly HashSet<string> Writers=new HashSet<string>(new[]{"S-1-5-18","S-1-5-32-544"},StringComparer.Ordinal);
  public static string Hash(string path){using(var f=new FileStream(path,FileMode.Open,FileAccess.Read,FileShare.Read))using(var h=SHA256.Create()){if(f.Length>67108864)throw new InvalidOperationException();return BitConverter.ToString(h.ComputeHash(f)).Replace("-","").ToLowerInvariant();}}
  public static void Descriptor(RawSecurityDescriptor sd,long mask,bool ignoreInheritOnly,bool osRootOwner=false){
   if(sd.Owner==null||(!Writers.Contains(sd.Owner.Value)&&!(osRootOwner&&sd.Owner.Value=="S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"))||sd.DiscretionaryAcl==null||sd.DiscretionaryAcl.Count==0)throw new InvalidOperationException();
   foreach(GenericAce raw in sd.DiscretionaryAcl){CommonAce a=raw as CommonAce;if(a==null||a.IsCallback)throw new InvalidOperationException();if(ignoreInheritOnly&&(a.AceFlags&AceFlags.InheritOnly)!=0)continue;if(a.AceQualifier==AceQualifier.AccessAllowed&&(((long)a.AccessMask&0xffffffffL)&mask)!=0&&!Writers.Contains(a.SecurityIdentifier.Value))throw new InvalidOperationException();}
  }
  public static void PathAcl(string path){Settings.CheckPath(path);FileSystemInfo entry=Directory.Exists(path)?(FileSystemInfo)new DirectoryInfo(path):new FileInfo(path);int depth=0;while(entry!=null){if(!entry.Exists||(entry.Attributes&FileAttributes.ReparsePoint)!=0)throw new InvalidOperationException();FileSystemSecurity acl=entry is DirectoryInfo?(FileSystemSecurity)((DirectoryInfo)entry).GetAccessControl(AccessControlSections.Owner|AccessControlSections.Access):((FileInfo)entry).GetAccessControl(AccessControlSections.Owner|AccessControlSections.Access);var sd=new RawSecurityDescriptor(acl.GetSecurityDescriptorBinaryForm(),0);Descriptor(sd,depth<=1?1343029590L:269287488L,depth>1,Settings.IsTrustedOsRootOwner(sd.Owner==null?null:sd.Owner.Value,entry.FullName,Path.GetPathRoot(Environment.GetFolderPath(Environment.SpecialFolder.Windows)),depth));DirectoryInfo d=entry as DirectoryInfo;entry=d!=null?d.Parent:((FileInfo)entry).Directory;depth++;}}
  public static void Tree(string root){int count=1;var pending=new Stack<string>();pending.Push(root);while(pending.Count!=0){string p=pending.Pop();PathAcl(p);if(Directory.Exists(p))foreach(string child in Directory.EnumerateFileSystemEntries(p)){if(++count>8192)throw new InvalidOperationException();pending.Push(child);}}}
  public static void Bound(string path,string expected){PathAcl(path);if(Hash(path)!=expected)throw new InvalidOperationException();PathAcl(path);}
  public static void JournalRoot(string plan,string service){
   string parent=Path.GetDirectoryName(plan),root=Path.Combine(parent,"journal");PathAcl(parent);
   var directory=new DirectoryInfo(root);if(!directory.Exists||(directory.Attributes&FileAttributes.ReparsePoint)!=0)throw new InvalidOperationException();
   var items=new List<FileSystemInfo>();items.Add(directory);int count=0;
   foreach(var item in directory.EnumerateFileSystemInfos()){
    if(++count>64||item is DirectoryInfo||!Regex.IsMatch(item.Name,"\\A(?:controller-state\\.(?:json|lock)|\\.ha-journal-[a-z0-9_]{8})\\z"))throw new InvalidOperationException();items.Add(item);
   }
   foreach(var item in items){
    if((item.Attributes&FileAttributes.ReparsePoint)!=0)throw new InvalidOperationException();
    FileSystemSecurity acl=item is DirectoryInfo?(FileSystemSecurity)((DirectoryInfo)item).GetAccessControl(AccessControlSections.Owner|AccessControlSections.Access):((FileInfo)item).GetAccessControl(AccessControlSections.Owner|AccessControlSections.Access);
    var sd=new RawSecurityDescriptor(acl.GetSecurityDescriptorBinaryForm(),0);
    if(sd.Owner==null||(!Writers.Contains(sd.Owner.Value)&&sd.Owner.Value!=service)||sd.DiscretionaryAcl==null||sd.DiscretionaryAcl.Count==0)throw new InvalidOperationException();
    long granted=0;
    foreach(GenericAce raw in sd.DiscretionaryAcl){var a=raw as CommonAce;if(a==null||a.IsCallback)throw new InvalidOperationException();if((a.AceFlags&AceFlags.InheritOnly)!=0)continue;long mask=(long)a.AccessMask&0xffffffffL;if(a.AceQualifier==AceQualifier.AccessDenied&&(mask&0xd00d0157L)!=0)throw new InvalidOperationException();if(a.AceQualifier==AceQualifier.AccessAllowed){if((mask&0xd00d0157L)!=0&&!Writers.Contains(a.SecurityIdentifier.Value)&&a.SecurityIdentifier.Value!=service)throw new InvalidOperationException();if(a.SecurityIdentifier.Value==service)granted|=mask;}}
    if((granted&0x10000000L)==0&&(granted&197055L)!=197055L)throw new InvalidOperationException();
   }
  }
  public static void PrivateRead(string path,string reader){
   PathAcl(path);
   foreach(string p in new[]{path,Path.GetDirectoryName(path)}){
    FileSystemSecurity acl=Directory.Exists(p)?(FileSystemSecurity)new DirectoryInfo(p).GetAccessControl(AccessControlSections.Owner|AccessControlSections.Access):new FileInfo(p).GetAccessControl(AccessControlSections.Owner|AccessControlSections.Access);
    var sd=new RawSecurityDescriptor(acl.GetSecurityDescriptorBinaryForm(),0);
    foreach(GenericAce raw in sd.DiscretionaryAcl){var a=raw as CommonAce;if(a==null||a.IsCallback)throw new InvalidOperationException();if((a.AceFlags&AceFlags.InheritOnly)!=0)continue;if(a.AceQualifier==AceQualifier.AccessAllowed&&(((long)a.AccessMask&0xffffffffL)&0x90000001L)!=0&&!Writers.Contains(a.SecurityIdentifier.Value)&&a.SecurityIdentifier.Value!=reader)throw new InvalidOperationException();}
   }
  }
 }
 // These handles are owned only by this host, never PID-based cleanup.
 sealed class OwnedJob : IDisposable {
  [StructLayout(LayoutKind.Sequential)] struct Basic {public long User,Kernel;public uint Flags;public UIntPtr Min,Max;public uint Active;public UIntPtr Affinity;public uint Priority,Scheduling;}
  [StructLayout(LayoutKind.Sequential)] struct Io {public ulong A,B,C,D,E,F;}
  [StructLayout(LayoutKind.Sequential)] struct Extended {public Basic Basic;public Io Io;public UIntPtr ProcessMemory,JobMemory,PeakProcess,PeakJob;}
  [StructLayout(LayoutKind.Sequential,CharSet=CharSet.Unicode)] struct Startup {public int Size;public string Reserved,Desktop,Title;public uint X,Y,XS,YS,XC,YC,Fill,Flags;public short Show,Reserved2;public IntPtr ReservedPtr,Input,Output,Error;}
  [StructLayout(LayoutKind.Sequential)] struct ProcessInfo {public IntPtr Process,Thread;public uint Pid,Tid;}
  [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern IntPtr CreateJobObject(IntPtr security,string name);
  [DllImport("kernel32.dll",SetLastError=true)] static extern bool SetInformationJobObject(IntPtr job,int type,ref Extended info,uint size);
  [DllImport("kernel32.dll",SetLastError=true)] static extern bool AssignProcessToJobObject(IntPtr job,IntPtr process);
  [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern bool CreateProcess(string app,StringBuilder command,IntPtr ps,IntPtr ts,bool inherit,uint flags,IntPtr env,string cwd,ref Startup startup,out ProcessInfo pi);
  [DllImport("kernel32.dll")] static extern uint ResumeThread(IntPtr thread);
  [DllImport("kernel32.dll")] static extern bool TerminateProcess(IntPtr process,uint code);
  [DllImport("kernel32.dll")] static extern bool TerminateJobObject(IntPtr job,uint code);
  [DllImport("kernel32.dll")] static extern uint WaitForSingleObject(IntPtr handle,uint ms);
  [DllImport("kernel32.dll",SetLastError=true)] static extern bool GetExitCodeProcess(IntPtr handle,out uint code);
  [DllImport("kernel32.dll")] static extern IntPtr GetCurrentProcess();
  [DllImport("kernel32.dll",SetLastError=true)] static extern bool DuplicateHandle(IntPtr sourceProcess,IntPtr source,IntPtr targetProcess,out IntPtr target,uint access,bool inherit,uint options);
  [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
  readonly object handles=new object();IntPtr job,process;
  public OwnedJob(Settings settings){
   job=CreateJobObject(IntPtr.Zero,null);if(job==IntPtr.Zero)throw new InvalidOperationException();
   try{var limit=new Extended();limit.Basic.Flags=0x2000; // KILL_ON_JOB_CLOSE; no breakaway flags.
    if(!SetInformationJobObject(job,9,ref limit,(uint)Marshal.SizeOf(typeof(Extended))))throw new InvalidOperationException();
    var si=new Startup();si.Size=Marshal.SizeOf(typeof(Startup));ProcessInfo pi;
    string system=Environment.GetFolderPath(Environment.SpecialFolder.Windows);
    string block="PATH="+Path.Combine(system,"System32")+"\0SystemRoot="+system+"\0\0";IntPtr env=Marshal.StringToHGlobalUni(block);
    try{if(!CreateProcess(settings.Values["python_path"],new StringBuilder(settings.CommandLine()),IntPtr.Zero,IntPtr.Zero,false,0x404,env,Path.GetDirectoryName(settings.Values["launcher_path"]),ref si,out pi))throw new InvalidOperationException();}finally{Marshal.FreeHGlobal(env);}
    process=pi.Process;
    try{if(!AssignProcessToJobObject(job,process)||ResumeThread(pi.Thread)==0xffffffff){TerminateProcess(process,1);throw new InvalidOperationException();}}finally{CloseHandle(pi.Thread);}
   }catch{Dispose();throw;}
  }
  public uint Wait(){IntPtr copy;lock(handles){if(process==IntPtr.Zero||!DuplicateHandle(GetCurrentProcess(),process,GetCurrentProcess(),out copy,0,false,2))throw new InvalidOperationException();}try{uint code;if(WaitForSingleObject(copy,0xffffffff)!=0||!GetExitCodeProcess(copy,out code)||code==259)throw new InvalidOperationException();return code;}finally{CloseHandle(copy);}}
  public void Dispose(){lock(handles){if(job!=IntPtr.Zero){TerminateJobObject(job,1);CloseHandle(job);job=IntPtr.Zero;}if(process!=IntPtr.Zero){if(WaitForSingleObject(process,5000)!=0){TerminateProcess(process,1);if(WaitForSingleObject(process,5000)!=0)Environment.FailFast("owned_child_cleanup_failed");}CloseHandle(process);process=IntPtr.Zero;}}}
 }
 public sealed class Host : ServiceBase {
  readonly Settings settings;readonly string argsPath,argsHash,selfPath;readonly object gate=new object();OwnedJob child;bool stopping,successfulOneShot;
  Host(Settings s,string args,string digest,string self){settings=s;argsPath=args;argsHash=digest;selfPath=self;ServiceName=s.Values["service_name"];AutoLog=false;CanStop=true;CanShutdown=true;}
  void Validate(){
   using(var identity=WindowsIdentity.GetCurrent()){if(identity.User==null||identity.User.Value!=settings.Values["service_sid"]||Process.GetCurrentProcess().SessionId!=0||((SecurityIdentifier)new NTAccount("NT SERVICE\\"+ServiceName).Translate(typeof(SecurityIdentifier))).Value!=identity.User.Value)throw new InvalidOperationException();}
   Protection.Bound(argsPath,argsHash);Protection.Bound(selfPath,settings.Values["host_sha256"]);
   Protection.JournalRoot(settings.Values["plan_path"],settings.Values["service_sid"]);
   foreach(string k in new[]{"python","launcher","plan","approval"})Protection.Bound(settings.Values[k+"_path"],settings.Values[k+"_sha256"]);
   foreach(string p in new[]{argsPath,settings.Values["plan_path"],settings.Values["approval_path"]})Protection.PrivateRead(p,settings.Values["service_sid"]);
   // Python's isolated standard/site libraries and controller import root are
   // protected too, not just the entry executable/script.
   Protection.Tree(Path.GetDirectoryName(settings.Values["python_path"]));
   Protection.Tree(Directory.GetParent(Path.GetDirectoryName(settings.Values["launcher_path"])).FullName);
  }
  protected override void OnStart(string[] args){if(args.Length!=0)throw new InvalidOperationException();lock(gate){try{if(stopping||child!=null)throw new InvalidOperationException();Validate();var owned=new OwnedJob(settings);child=owned;ThreadPool.QueueUserWorkItem(delegate{uint code=1;bool completed=false,requestStop=false;try{code=owned.Wait();completed=true;}catch{}finally{lock(gate){if(!stopping&&Object.ReferenceEquals(child,owned)){ExitCode=completed?Settings.ServiceExitCode(settings.Values["mode"],code):1;successfulOneShot=ExitCode==0;requestStop=true;}}if(requestStop)Stop();}});}catch{if(child!=null){child.Dispose();child=null;}throw new InvalidOperationException("enforced_service_blocked");}}}
  protected override void OnStop(){lock(gate){if(!successfulOneShot)ExitCode=1;stopping=true;if(child!=null){child.Dispose();child=null;}}}
  protected override void OnShutdown(){OnStop();base.OnShutdown();}
  public static int Main(string[] args){try{if(args.Length!=0||Environment.OSVersion.Platform!=PlatformID.Win32NT||!Environment.Is64BitProcess||Process.GetCurrentProcess().SessionId!=0)return 1;string reader;using(var id=WindowsIdentity.GetCurrent()){reader=id.User.Value;if(!Regex.IsMatch(reader,"\\AS-1-5-80-(?:[0-9]{1,10}-){4}[0-9]{1,10}\\z"))return 1;}string self=Process.GetCurrentProcess().MainModule.FileName;string path=Path.Combine(Path.GetDirectoryName(self),"service.args");Protection.PrivateRead(path,reader);if(new FileInfo(path).Length>16384)return 1;byte[] bytes=File.ReadAllBytes(path);if(bytes.Length>16384)return 1;string hash;using(var digest=SHA256.Create()){hash=BitConverter.ToString(digest.ComputeHash(bytes)).Replace("-","").ToLowerInvariant();}Settings s=new Settings(new UTF8Encoding(false,true).GetString(bytes));if(Protection.Hash(path)!=hash||s.Values["service_sid"]!=reader)return 1;ServiceBase.Run(new Host(s,path,hash,self));return 0;}catch{return 1;}}
 }
#endif
}
