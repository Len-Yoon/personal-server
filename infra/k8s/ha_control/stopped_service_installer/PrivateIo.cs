using System;using System.IO;using System.Text;using System.Runtime.InteropServices;using System.Security.AccessControl;using System.Security.Cryptography;using Microsoft.Win32.SafeHandles;
namespace TrustedStoppedV1 {
 public sealed class Held:IDisposable {
  [StructLayout(LayoutKind.Sequential)]struct Info{public uint Attr;public System.Runtime.InteropServices.ComTypes.FILETIME A,B,C;public uint Volume,High,Low,Links,IndexHigh,IndexLow;}
  [DllImport("kernel32.dll",CharSet=CharSet.Unicode,ExactSpelling=true,SetLastError=true)]static extern SafeFileHandle CreateFileW(string path,uint access,uint share,IntPtr sa,uint disposition,uint flags,IntPtr template);
  [DllImport("kernel32.dll",SetLastError=true)]static extern bool GetFileInformationByHandle(SafeFileHandle h,out Info info);
  SafeFileHandle handle;FileStream stream;readonly bool directory;public readonly string Identity;public byte[] Bytes;
  public Held(string path,bool dir){directory=dir;handle=CreateFileW(path,dir?0x20000U:0x80020000U,1,IntPtr.Zero,3,0x00200000U|(dir?0x02000000U:0),IntPtr.Zero);Info info;if(handle.IsInvalid||!GetFileInformationByHandle(handle,out info)||(info.Attr&0x400)!=0||((info.Attr&16)!=0)!=dir||(!dir&&info.Links!=1)){Dispose();throw new Failure(Marshal.GetLastWin32Error());}Identity=info.Volume.ToString("x8")+":"+info.IndexHigh.ToString("x8")+info.IndexLow.ToString("x8");if(!dir){stream=new FileStream(handle,FileAccess.Read);Contract.Need(stream.Length>=0&&stream.Length<=67108864);Bytes=new byte[(int)stream.Length];int at=0;while(at<Bytes.Length){int n=stream.Read(Bytes,at,Bytes.Length-at);Contract.Need(n>0);at+=n;}}}
  public void Verify(){Info i=new Info();Contract.Need(handle!=null&&!handle.IsClosed&&GetFileInformationByHandle(handle,out i));Contract.Need((i.Attr&0x400)==0&&(directory||i.Links==1)&&Identity==i.Volume.ToString("x8")+":"+i.IndexHigh.ToString("x8")+i.IndexLow.ToString("x8"));if(!directory){stream.Position=0;using(var h=SHA256.Create())Contract.Need(Hash(Bytes)==BitConverter.ToString(h.ComputeHash(stream)).Replace("-","").ToLowerInvariant());}}
  public static string Hash(byte[] b){using(var h=SHA256.Create())return BitConverter.ToString(h.ComputeHash(b)).Replace("-","").ToLowerInvariant();}
  public void Dispose(){if(stream!=null){stream.Dispose();stream=null;}if(handle!=null){handle.Dispose();handle=null;}}
 }
 public static class NewPrivate {
  [StructLayout(LayoutKind.Sequential)]struct Sa{public int Size;public IntPtr Descriptor;[MarshalAs(UnmanagedType.Bool)]public bool Inherit;}
  [DllImport("kernel32.dll",CharSet=CharSet.Unicode,ExactSpelling=true,SetLastError=true)]static extern bool CreateDirectoryW(string path,ref Sa sa);
  [DllImport("kernel32.dll",CharSet=CharSet.Unicode,ExactSpelling=true,SetLastError=true)]static extern SafeFileHandle CreateFileW(string path,uint access,uint share,ref Sa sa,uint disposition,uint flags,IntPtr template);
  // PowerShell can bind $null to an empty .NET string. Both represent an absent service SID.
  static string ServiceAce(string sid,string flags,string mask){
   return String.IsNullOrEmpty(sid)?"":"(A;"+flags+";"+mask+";;;"+sid+")";
  }
  public static string Acl(string sid,bool journal){
   return "O:BAG:SYD:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"+ServiceAce(sid,"OICI",journal?"0x301bf":"0x1200a9");
  }
  public static string FileAcl(string sid){
   return "O:BAG:SYD:P(A;;FA;;;SY)(A;;FA;;;BA)"+ServiceAce(sid,"","0x1200a9");
  }
  public static string InstallationRootAcl(string sid,string profile){Contract.Need(profile=="strict-canonical-observed-v1"&&System.Text.RegularExpressions.Regex.IsMatch(sid??"","\\AS-1-5-80-(?:[0-9]{1,10}-){4}[0-9]{1,10}\\z"));return "O:BAG:SYD:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;;0x200a0;;;"+sid+")";}
  public static void ValidateInstallationRootFacts(string profile,string expectedSid,string owner,int control,string[] sids,int[] masks,byte[] flags){Contract.Need(profile=="strict-canonical-observed-v1"&&(owner=="S-1-5-18"||owner=="S-1-5-32-544")&&(control&0x1004)==0x1004&&sids!=null&&masks!=null&&flags!=null&&sids.Length==3&&masks.Length==3&&flags.Length==3);bool sy=false,ba=false,service=false;for(int i=0;i<3;i++){if(sids[i]=="S-1-5-18"){Contract.Need(!sy&&masks[i]==2032127&&flags[i]==3);sy=true;}else if(sids[i]=="S-1-5-32-544"){Contract.Need(!ba&&masks[i]==2032127&&flags[i]==3);ba=true;}else{Contract.Need(!service&&sids[i]==expectedSid&&masks[i]==131232&&flags[i]==0);service=true;}}Contract.Need(sy&&ba&&service);}
  public static void InstallationRoot(string path,string sid,string profile){DirectoryAcl(path,InstallationRootAcl(sid,profile));}
  static byte[] Descriptor(string sddl){var sd=new RawSecurityDescriptor(sddl);var b=new byte[sd.BinaryLength];sd.GetBinaryForm(b,0);return b;}
  public static void Directory(string path,string sid,bool journal){DirectoryAcl(path,Acl(sid,journal));}
  static void DirectoryAcl(string path,string sddl){byte[] b=Descriptor(sddl);var pin=GCHandle.Alloc(b,GCHandleType.Pinned);try{var sa=new Sa{Size=Marshal.SizeOf(typeof(Sa)),Descriptor=pin.AddrOfPinnedObject()};if(!CreateDirectoryW(path,ref sa))throw new Failure(Marshal.GetLastWin32Error());}finally{pin.Free();}}
  public static void File(string path,byte[] bytes,string sid,bool readOnly){byte[] b=Descriptor(FileAcl(sid));var pin=GCHandle.Alloc(b,GCHandleType.Pinned);try{var sa=new Sa{Size=Marshal.SizeOf(typeof(Sa)),Descriptor=pin.AddrOfPinnedObject()};using(var h=CreateFileW(path,0x40000000,1,ref sa,1,0x80000000,IntPtr.Zero)){if(h.IsInvalid)throw new Failure(Marshal.GetLastWin32Error());using(var stream=new FileStream(h,FileAccess.Write)){stream.Write(bytes,0,bytes.Length);stream.Flush(true);}}if(readOnly)System.IO.File.SetAttributes(path,FileAttributes.ReadOnly);}finally{pin.Free();}}
 }
}
