using System;
using System.Text;
using System.Collections.Generic;
using System.Security.Cryptography;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
namespace TrustedStoppedV1 {
 public sealed class Failure:Exception { public readonly int NativeCode;public Failure(int code):base("stopped_install_preserve"){NativeCode=code;} }
 public sealed class Snapshot {public string Name,Image,Account;public byte[] SecurityDescriptor;public uint Type,Start,ErrorControl,State,Pid,SidType,Triggers,Actions;public bool Delayed,NonCrash;}
 public interface IApi:IDisposable {bool Exists(string name);void Create(string name,string image);void Seal(string name);void Sid();Snapshot Read(string name);}
 public static class Contract {
  const string AdministratorSddl="O:BAG:SYD:P(A;;CCDCLCSWRPWPDTLOCRSDRCWDWO;;;SY)(A;;CCDCLCSWRPWPDTLOCRSDRCWDWO;;;BA)";
  public static void Need(bool b){if(!b)throw new Failure(0);}
  public static void LocalPath(string path){Need(path!=null&&path.Length<=240&&System.Text.RegularExpressions.Regex.IsMatch(path,"\\A[A-Za-z]:\\\\")&&path.IndexOf('/')<0&&path.IndexOf('"')<0&&path.IndexOf('\r')<0&&path.IndexOf('\n')<0&&path.IndexOf('\0')<0&&path.Substring(2).IndexOf(':')<0);foreach(string part in path.Substring(3).Split('\\'))Need(part!=""&&part!="."&&part!=".."&&!part.EndsWith(".")&&!part.EndsWith(" ")&&!System.Text.RegularExpressions.Regex.IsMatch(part,"\\A(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\\.|\\z)",System.Text.RegularExpressions.RegexOptions.IgnoreCase));}
  public static void Inputs(string installation,string name,string host){Need(System.Text.RegularExpressions.Regex.IsMatch(installation??"","\\A[0-9a-f]{64}\\z"));Need(name=="N100HA_"+installation.Substring(0,24)+"_controller");LocalPath(host);Need(host.EndsWith("\\windows-service-host.exe",StringComparison.Ordinal));}
  public static string CanonicalInstallationRoot(string parent,string installation){LocalPath(parent);Need(System.Text.RegularExpressions.Regex.IsMatch(installation??"","\\A[0-9a-f]{64}\\z"));return parent+"\\N100HA_"+installation.Substring(0,24);}
  public static string ServiceSid(string name) {
   Need(System.Text.RegularExpressions.Regex.IsMatch(name??"","\\AN100HA_[0-9a-f]{24}_controller\\z"));
   byte[] digest;
   using(var sha=SHA1.Create())digest=sha.ComputeHash(Encoding.Unicode.GetBytes(name.ToUpperInvariant()));
   var sid=new StringBuilder("S-1-5-80");
   for(int i=0;i<digest.Length;i+=4){
    uint word=(uint)(digest[i]|digest[i+1]<<8|digest[i+2]<<16|digest[i+3]<<24);
    sid.Append('-').Append(word.ToString(System.Globalization.CultureInfo.InvariantCulture));
   }
   return sid.ToString();
  }
  public static string ServiceSddl(string name) {
   return AdministratorSddl+"(A;;0x20000;;;"+ServiceSid(name)+")";
  }
  public static void VerifyDaclFacts(string name,string owner,int control,string[] sids,int[] masks,byte[] flags) {
   string serviceSid=ServiceSid(name);
   Need(owner=="S-1-5-32-544"&&(control&0x1004)==0x1004);
   Need(sids!=null&&masks!=null&&flags!=null&&sids.Length==3&&masks.Length==3&&flags.Length==3);
   var expected=new Dictionary<string,int>(StringComparer.Ordinal){
    {"S-1-5-18",0xF01FF},{"S-1-5-32-544",0xF01FF},{serviceSid,0x20000}
   };
   for(int i=0;i<sids.Length;i++){
    int mask;
    Need(sids[i]!=null);
    Need(expected.TryGetValue(sids[i],out mask)&&masks[i]==mask&&flags[i]==0);
    expected.Remove(sids[i]);
   }
   Need(expected.Count==0);
  }
  public static void VerifySecurityDescriptor(string name,byte[] bytes) {
   Need(bytes!=null);
   var descriptor=new RawSecurityDescriptor(bytes,0);
   Need(descriptor.Owner!=null&&descriptor.DiscretionaryAcl!=null&&descriptor.DiscretionaryAcl.Count==3);
   var sids=new string[3];var masks=new int[3];var flags=new byte[3];
   for(int i=0;i<3;i++){
    var ace=descriptor.DiscretionaryAcl[i] as CommonAce;
    Need(ace!=null&&!ace.IsCallback&&ace.AceQualifier==AceQualifier.AccessAllowed);
    sids[i]=ace.SecurityIdentifier.Value;
    masks[i]=ace.AccessMask;
    flags[i]=(byte)ace.AceFlags;
   }
   VerifyDaclFacts(name,descriptor.Owner.Value,(int)descriptor.ControlFlags,sids,masks,flags);
  }
  public static void VerifyFields(Snapshot s,string name,string image){Need(s!=null&&s.Name==name&&s.Image==image&&String.Equals(s.Account,"NT SERVICE\\"+name,StringComparison.OrdinalIgnoreCase)&&s.Type==16&&s.Start==3&&s.ErrorControl==1&&s.State==1&&s.Pid==0&&s.SidType==1&&!s.Delayed&&!s.NonCrash&&s.Triggers==0&&s.Actions==0);}
  public static void Verify(Snapshot s,string name,string image){VerifyFields(s,name,image);VerifySecurityDescriptor(name,s.SecurityDescriptor);}
  // Caller must durably CreateNew its authority-bound intent BEFORE this method.
  // Only a fresh CreateService object can be sealed. No adoption or recovery.
  public static Snapshot Register(IApi api,string installation,string name,string host,Action<string> durableEvent){Inputs(installation,name,host);Need(durableEvent!=null);if(api.Exists(name))throw new Failure(1073);string image="\""+host+"\"";durableEvent("before_create");api.Create(name,image);durableEvent("created");api.Seal(name);durableEvent("dacl_sealed");api.Sid();durableEvent("sid_type_set");Snapshot s=api.Read(name);Verify(s,name,image);durableEvent("manual_stopped_readback_verified");return s;}
 }
 public sealed class Native:IApi {
  IntPtr scm,owned;
  [StructLayout(LayoutKind.Sequential)]struct Config{public uint Type,Start,Error;public IntPtr Binary,Group;public uint Tag;public IntPtr Dependencies,Account,Display;}
  [StructLayout(LayoutKind.Sequential)]struct Status{public uint Type,State,Controls,Win32Exit,SpecificExit,Checkpoint,WaitHint,Pid,Flags;}
  [StructLayout(LayoutKind.Sequential)]struct Actions{public uint Reset;public IntPtr Reboot,Command;public uint Count;public IntPtr List;}
  [DllImport("advapi32.dll",CharSet=CharSet.Unicode,ExactSpelling=true,SetLastError=true)]static extern IntPtr OpenSCManagerW(string machine,string database,uint access);
  [DllImport("advapi32.dll",CharSet=CharSet.Unicode,ExactSpelling=true,SetLastError=true)]static extern IntPtr OpenServiceW(IntPtr scm,string name,uint access);
  [DllImport("advapi32.dll",CharSet=CharSet.Unicode,ExactSpelling=true,SetLastError=true)]static extern IntPtr CreateServiceW(IntPtr scm,string name,string display,uint access,uint type,uint start,uint error,string image,string group,IntPtr tag,string dependencies,string account,string password);
  [DllImport("advapi32.dll",SetLastError=true)]static extern bool CloseServiceHandle(IntPtr h);
  [DllImport("advapi32.dll")]static extern uint SetSecurityInfo(IntPtr h,uint objectType,uint information,IntPtr owner,IntPtr group,IntPtr dacl,IntPtr sacl);
  [DllImport("advapi32.dll",SetLastError=true)]static extern bool QueryServiceObjectSecurity(IntPtr h,uint information,byte[] descriptor,uint size,out uint needed);
  [DllImport("advapi32.dll",ExactSpelling=true,SetLastError=true)]static extern bool QueryServiceConfigW(IntPtr h,IntPtr buffer,uint size,out uint needed);
  [DllImport("advapi32.dll",ExactSpelling=true,SetLastError=true)]static extern bool QueryServiceConfig2W(IntPtr h,uint level,IntPtr buffer,uint size,out uint needed);
  [DllImport("advapi32.dll",ExactSpelling=true,SetLastError=true)]static extern bool ChangeServiceConfig2W(IntPtr h,uint level,ref uint value);
  [DllImport("advapi32.dll",SetLastError=true)]static extern bool QueryServiceStatusEx(IntPtr h,int level,out Status status,uint size,out uint needed);
  static void Check(bool b){if(!b)throw new Failure(Marshal.GetLastWin32Error());}
  public Native(){scm=OpenSCManagerW(null,null,3);if(scm==IntPtr.Zero)throw new Failure(Marshal.GetLastWin32Error());}
  public void OpenForRead(string name){Contract.Need(owned==IntPtr.Zero);owned=OpenServiceW(scm,name,0x20005);Check(owned!=IntPtr.Zero);}
  public bool Exists(string name){IntPtr h=OpenServiceW(scm,name,4);if(h!=IntPtr.Zero){CloseServiceHandle(h);return true;}int e=Marshal.GetLastWin32Error();if(e!=1060)throw new Failure(e);return false;}
  public void Create(string name,string image){Contract.Need(owned==IntPtr.Zero);owned=CreateServiceW(scm,name,name,0xE0007,16,3,1,image,null,IntPtr.Zero,null,"NT SERVICE\\"+name,null);if(owned==IntPtr.Zero)throw new Failure(Marshal.GetLastWin32Error());}
  public void Seal(string name){Contract.Need(owned!=IntPtr.Zero);var sd=new RawSecurityDescriptor(Contract.ServiceSddl(name));byte[] b=new byte[sd.BinaryLength];sd.GetBinaryForm(b,0);var pin=GCHandle.Alloc(b,GCHandleType.Pinned);try{IntPtr p=pin.AddrOfPinnedObject();uint result=SetSecurityInfo(owned,2,0x80000005U,IntPtr.Add(p,BitConverter.ToInt32(b,4)),IntPtr.Zero,IntPtr.Add(p,BitConverter.ToInt32(b,16)),IntPtr.Zero);if(result!=0)throw new Failure((int)result);}finally{pin.Free();}}
  public void Sid(){Contract.Need(owned!=IntPtr.Zero);uint type=1;Check(ChangeServiceConfig2W(owned,5,ref type));}
  IntPtr Query(uint level){uint n;QueryServiceConfig2W(owned,level,IntPtr.Zero,0,out n);Contract.Need(Marshal.GetLastWin32Error()==122&&n>=4&&n<=65536);IntPtr p=Marshal.AllocHGlobal((int)n);try{Check(QueryServiceConfig2W(owned,level,p,n,out n));return p;}catch{Marshal.FreeHGlobal(p);throw;}}
  public Snapshot Read(string name){Contract.Need(owned!=IntPtr.Zero);uint n;QueryServiceConfigW(owned,IntPtr.Zero,0,out n);Contract.Need(Marshal.GetLastWin32Error()==122&&n>=Marshal.SizeOf(typeof(Config))&&n<=65536);IntPtr p=Marshal.AllocHGlobal((int)n);var s=new Snapshot();try{Check(QueryServiceConfigW(owned,p,n,out n));Config c=(Config)Marshal.PtrToStructure(p,typeof(Config));s.Name=name;s.Type=c.Type;s.Start=c.Start;s.ErrorControl=c.Error;s.Image=Marshal.PtrToStringUni(c.Binary);s.Account=Marshal.PtrToStringUni(c.Account);Contract.Need(c.Group==IntPtr.Zero||Marshal.PtrToStringUni(c.Group)=="");Contract.Need(c.Dependencies==IntPtr.Zero||Marshal.ReadInt16(c.Dependencies)==0);}finally{Marshal.FreeHGlobal(p);}
   Status status;Check(QueryServiceStatusEx(owned,0,out status,(uint)Marshal.SizeOf(typeof(Status)),out n));s.State=status.State;s.Pid=status.Pid;Contract.Need(status.Type==16);
   foreach(uint level in new uint[]{3,4,5,8,2}){p=Query(level);try{if(level==3)s.Delayed=Marshal.ReadInt32(p)!=0;if(level==4)s.NonCrash=Marshal.ReadInt32(p)!=0;if(level==5)s.SidType=(uint)Marshal.ReadInt32(p);if(level==8)s.Triggers=(uint)Marshal.ReadInt32(p);if(level==2){Actions a=(Actions)Marshal.PtrToStructure(p,typeof(Actions));s.Actions=a.Count;Contract.Need(a.Reset==0&&a.Reboot==IntPtr.Zero&&a.Command==IntPtr.Zero);}}finally{Marshal.FreeHGlobal(p);}}
   QueryServiceObjectSecurity(owned,7,null,0,out n);Contract.Need(Marshal.GetLastWin32Error()==122&&n>=20&&n<=65536);byte[] b=new byte[n];Check(QueryServiceObjectSecurity(owned,7,b,n,out n));s.SecurityDescriptor=b;return s;
  }
  public void Dispose(){if(owned!=IntPtr.Zero){CloseServiceHandle(owned);owned=IntPtr.Zero;}if(scm!=IntPtr.Zero){CloseServiceHandle(scm);scm=IntPtr.Zero;}}
 }
}
