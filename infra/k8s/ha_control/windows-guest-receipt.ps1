param([string]$Request,[string]$RequestSha256,[switch]$Go)
# Read-only owned Gen2 COM collector. Never starts/stops a VM or changes ACLs.
# MS API contracts: Msvm_ComputerSystem.ProcessID; GetNamedPipeServerProcessId;
# GetSecurityInfo(retained handle). FILE_CREATE_PIPE_INSTANCE == FILE_APPEND_DATA.
# https://learn.microsoft.com/en-us/windows/win32/hyperv_v2/msvm-computersystem
# https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-getnamedpipeserverprocessid
# https://learn.microsoft.com/en-us/windows/win32/ipc/named-pipe-security-and-access-rights
# Run as a dedicated -NoProfile -NonInteractive -File process; ready process
# liveness is a Host/Admin-TCB gate, never proof of an authenticated pipe.
$ErrorActionPreference='Stop'
$WarningPreference='SilentlyContinue'
$VerbosePreference='SilentlyContinue'
$InformationPreference='SilentlyContinue'
$DebugPreference='SilentlyContinue'
Set-StrictMode -Version Latest
$script:GuestComInitialized=$false
$script:GuestComSource=@'
using System;
using System.IO;
using System.IO.Pipes;
using System.Text;
using System.Text.RegularExpressions;
using System.Collections.Generic;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Security.Cryptography;
using System.Runtime.InteropServices;
using System.Threading.Tasks;
using Microsoft.Win32.SafeHandles;
namespace HaGuestComV1 {
 public static class Json {
  sealed class Parser {
   readonly string text; int at; int nodes;
   public Parser(string s){text=s;}
   void White(){while(at<text.Length&&(text[at]==' '||text[at]=='\r'||text[at]=='\n'||text[at]=='\t'))at++;}
   void Need(bool ok){if(!ok)throw new InvalidOperationException("guest_com_json_rejected");}
   string String(){
    Need(at<text.Length&&text[at++]=='"');var b=new StringBuilder();bool done=false;
    while(at<text.Length){char c=text[at++];if(c=='"'){done=true;break;}
     if(c=='\\'){Need(at<text.Length);c=text[at++];if(c=='u'){Need(at+4<=text.Length);int value=0;
       for(int i=0;i<4;i++){char h=text[at++];int n=h>='0'&&h<='9'?h-'0':h>='a'&&h<='f'?h-'a'+10:h>='A'&&h<='F'?h-'A'+10:-1;Need(n>=0);value=value*16+n;}c=(char)value;
      }else Need(c=='"'||c=='\\'||c=='/');}
     Need(c>=32&&c<=126);b.Append(c);Need(b.Length<=4096);
    }Need(done);return b.ToString();
   }
   object Value(int depth){
    Need(depth<=8&&++nodes<=512);White();Need(at<text.Length);char c=text[at];
    if(c=='"')return String();
    if(c=='{'){at++;White();var result=new Dictionary<string,object>(StringComparer.Ordinal);
     if(at<text.Length&&text[at]=='}'){at++;return result;}
     while(true){White();string key=String();Need(key.Length>0&&key.Length<=80&&!result.ContainsKey(key));White();Need(at<text.Length&&text[at++]==':');result.Add(key,Value(depth+1));White();Need(at<text.Length);char end=text[at++];if(end=='}')return result;Need(end==',');}
    }
    if(c=='['){at++;White();var values=new List<object>();if(at<text.Length&&text[at]==']'){at++;return values;}
     while(true){Need(values.Count<128);values.Add(Value(depth+1));White();Need(at<text.Length);char end=text[at++];if(end==']')return values;Need(end==',');}}
    foreach(string word in new string[]{"true","false","null"})if(at+word.Length<=text.Length&&System.String.CompareOrdinal(text,at,word,0,word.Length)==0){at+=word.Length;return word=="null"?null:(object)(word=="true");}
    int start=at;if(c=='-')at++;Need(at<text.Length&&text[at]>='0'&&text[at]<='9');
    if(text[at]=='0')at++;else while(at<text.Length&&text[at]>='0'&&text[at]<='9')at++;
    long integer;Need(Int64.TryParse(text.Substring(start,at-start),System.Globalization.NumberStyles.AllowLeadingSign,System.Globalization.CultureInfo.InvariantCulture,out integer));return integer;
   }
   public object Parse(){object value=Value(0);White();Need(at==text.Length);return value;}
  }
  public static Dictionary<string,object> Object(string text,int maximum){
   if(text==null||text.Length>maximum||Encoding.UTF8.GetByteCount(text)>maximum)throw new InvalidOperationException("guest_com_json_size");
   object value=new Parser(text).Parse();var result=value as Dictionary<string,object>;if(result==null)throw new InvalidOperationException("guest_com_json_object_required");return result;
  }
 }
 public static class Rules {
  public const string Prefix="N100_HA_SEED_RECEIPT ";
  public static void Require(bool value,string reason){if(!value)throw new InvalidOperationException(reason);}
  public static bool Match(string value,string pattern){return value!=null&&Regex.IsMatch(value,"\\A(?:"+pattern+")\\z",RegexOptions.CultureInvariant);}
  public static string Text(Dictionary<string,object> value,string key){object item;Require(value.TryGetValue(key,out item)&&item is string,"guest_com_value_type");return (string)item;}
  public static long Integer(Dictionary<string,object> value,string key){object item;Require(value.TryGetValue(key,out item)&&item is long,"guest_com_value_type");return (long)item;}
  public static bool Boolean(Dictionary<string,object> value,string key){object item;Require(value.TryGetValue(key,out item)&&item is bool,"guest_com_value_type");return (bool)item;}
  public static void Keys(Dictionary<string,object> value,string[] expected){Require(value.Count==expected.Length,"guest_com_shape_rejected");foreach(string key in expected)Require(value.ContainsKey(key),"guest_com_shape_rejected");}
  public static string Hash(byte[] bytes){using(var h=SHA256.Create())return BitConverter.ToString(h.ComputeHash(bytes)).Replace("-","").ToLowerInvariant();}
  public static bool TrustedSid(string sid,string caller){return sid=="S-1-5-18"||sid=="S-1-5-32-544"||sid==caller;}
  public static bool AllowPipeAce(string sid,uint mask,string caller){const uint read=0x80000000U|0x00100000U|0x00020000U|0x00000001U|0x00000008U|0x00000080U;return TrustedSid(sid,caller)||(mask&~read)==0;}
  public static string Fingerprint(string text){
   Require(text!=null&&Match(text,"ssh-ed25519 [A-Za-z0-9+/]{68}"),"guest_com_public_key_rejected");
   byte[] raw;try{raw=Convert.FromBase64String(text.Substring(12));}catch{throw new InvalidOperationException("guest_com_public_key_rejected");}
   Require(raw.Length==51&&raw[0]==0&&raw[1]==0&&raw[2]==0&&raw[3]==11&&Encoding.ASCII.GetString(raw,4,11)=="ssh-ed25519"&&raw[15]==0&&raw[16]==0&&raw[17]==0&&raw[18]==32,"guest_com_public_key_rejected");
   using(var h=SHA256.Create())return "SHA256:"+Convert.ToBase64String(h.ComputeHash(raw)).TrimEnd('=');
  }
  static string[] identity={"source_commit","installation_id","node","seed_id","bootstrap_manifest_sha256","expected_kernel_release"};
  public static Dictionary<string,object> Receipt(string payload,Dictionary<string,object> request){
   var value=Json.Object(payload,4096);string status=Text(value,"status");
   var keys=new List<string>(identity);keys.AddRange(new string[]{"status","production_deployed","pg_started","writer_authority_issued"});
   if(status=="PASS")keys.AddRange(new string[]{"scope","cloud_init_status_exit","kernel_boot_verified","guest_reboot_requests","new_boot_id_observed","boot_id_sha256","automatic_os_updates_disabled","package_count","pg_image","pg_state","provider_credentials_supplied_by_seed","effects_transition_performed","server_public_key","server_key_fingerprint","serial_delivery_is_not_host_receipt_verification"});
   else {Require(status=="FAIL","guest_com_guest_status_rejected");keys.Add("reason");if(value.ContainsKey("server_public_key")||value.ContainsKey("server_key_fingerprint"))keys.AddRange(new string[]{"server_public_key","server_key_fingerprint"});}
   Keys(value,keys.ToArray());
   foreach(string key in identity){string expected=Text(request,key=="source_commit"?"seed_source_commit":key);Require(Text(value,key)==expected,"guest_com_guest_identity_mismatch");}
   foreach(string key in new string[]{"production_deployed","pg_started","writer_authority_issued"})Require(!Boolean(value,key),"guest_com_guest_scope_rejected");
   if(status=="FAIL")Require(Match(Text(value,"reason"),"[a-z0-9_]{1,80}"),"guest_com_failure_reason_rejected");
   else {
    Require(Text(value,"scope")=="new_guest_os_engine_and_pg_image_not_started"&&Text(value,"pg_state")=="installed-not-started","guest_com_guest_scope_rejected");
    Require(Integer(value,"cloud_init_status_exit")==0&&Integer(value,"guest_reboot_requests")==1&&Integer(value,"package_count")==Integer(request,"expected_package_count"),"guest_com_guest_boot_rejected");
    foreach(string key in new string[]{"kernel_boot_verified","new_boot_id_observed","automatic_os_updates_disabled","serial_delivery_is_not_host_receipt_verification"})Require(Boolean(value,key),"guest_com_guest_boot_rejected");
    foreach(string key in new string[]{"provider_credentials_supplied_by_seed","effects_transition_performed"})Require(!Boolean(value,key),"guest_com_guest_scope_rejected");
    Require(Match(Text(value,"boot_id_sha256"),"[0-9a-f]{64}")&&Text(value,"pg_image")==Text(request,"expected_pg_image"),"guest_com_guest_runtime_mismatch");
   }
   if(value.ContainsKey("server_public_key"))Require(Fingerprint(Text(value,"server_public_key"))==Text(value,"server_key_fingerprint"),"guest_com_fingerprint_mismatch");
   return value;
  }
  public static void ProcessBinding(uint vmPid,uint serverPid,uint retainedPid,long before,long after,bool alive,string image,string expected,string owner){Require(vmPid>0&&vmPid==serverPid&&vmPid==retainedPid&&before>0&&before==after&&alive&&String.Equals(image,expected,StringComparison.OrdinalIgnoreCase)&&owner=="S-1-5-18","guest_com_vmwp_binding_rejected");}
 }
 public sealed class Framer {
  int matched;bool discard;readonly List<byte> candidate=new List<byte>();public long Bytes{get;private set;}public int Lines{get;private set;}
  public void ResetConnection(){matched=0;discard=false;candidate.Clear();}
  public string Feed(byte[] data,int count){
   Rules.Require(data!=null&&count>=0&&count<=data.Length,"guest_com_frame_rejected");string found=null;
   for(int i=0;i<count;i++){
    Rules.Require(++Bytes<=2097152,"guest_com_byte_limit");byte b=data[i];
    if(b==10){Rules.Require(++Lines<=8192,"guest_com_line_limit");if(!discard&&matched==Rules.Prefix.Length){Rules.Require(found==null,"guest_com_multiple_receipts");found=new UTF8Encoding(false,true).GetString(candidate.ToArray());}ResetConnection();continue;}
    if(discard)continue;
    if(matched<Rules.Prefix.Length){if(b==(byte)Rules.Prefix[matched])matched++;else discard=true;}
    else{Rules.Require(candidate.Count<4096,"guest_com_receipt_size");candidate.Add(b);}
   }return found;
  }
 }
#if !PORTABLE_FIXTURE
 public static class Native {
  [DllImport("kernel32.dll",SetLastError=true)]static extern bool GetNamedPipeServerProcessId(SafePipeHandle pipe,out uint pid);
  [DllImport("advapi32.dll",SetLastError=true)]static extern uint GetSecurityInfo(SafeHandle handle,int type,uint info,out IntPtr owner,out IntPtr group,out IntPtr dacl,out IntPtr sacl,out IntPtr descriptor);
  [DllImport("advapi32.dll")]static extern uint GetSecurityDescriptorLength(IntPtr descriptor);
  [DllImport("kernel32.dll")]static extern IntPtr LocalFree(IntPtr memory);
  [DllImport("kernel32.dll",SetLastError=true)]internal static extern IntPtr OpenProcess(uint access,bool inherit,uint pid);
  [DllImport("kernel32.dll",SetLastError=true)]internal static extern bool CloseHandle(IntPtr handle);
  [DllImport("kernel32.dll",SetLastError=true)]internal static extern uint WaitForSingleObject(IntPtr handle,uint milliseconds);
  [StructLayout(LayoutKind.Sequential)]public struct Time{public uint Low;public uint High;public long Value(){return unchecked((long)(((ulong)High<<32)|Low));}}
  [DllImport("kernel32.dll",SetLastError=true)]internal static extern bool GetProcessTimes(IntPtr handle,out Time creation,out Time exit,out Time kernel,out Time user);
  [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)]internal static extern bool QueryFullProcessImageName(IntPtr handle,uint flags,StringBuilder path,ref uint size);
  [DllImport("advapi32.dll",SetLastError=true)]static extern bool OpenProcessToken(IntPtr process,uint desired,out IntPtr token);
  [DllImport("advapi32.dll",SetLastError=true)]static extern bool GetTokenInformation(IntPtr token,int kind,IntPtr output,int length,out int needed);
  [StructLayout(LayoutKind.Sequential)]struct SidAndAttributes{public IntPtr Sid;public uint Attributes;}
  public static string ProcessOwner(IntPtr process){IntPtr token;Rules.Require(OpenProcessToken(process,8,out token),"guest_com_process_token_unknown");try{int length;GetTokenInformation(token,1,IntPtr.Zero,0,out length);Rules.Require(length>0&&length<=4096,"guest_com_process_token_unknown");IntPtr data=Marshal.AllocHGlobal(length);try{Rules.Require(GetTokenInformation(token,1,data,length,out length),"guest_com_process_token_unknown");return new SecurityIdentifier(((SidAndAttributes)Marshal.PtrToStructure(data,typeof(SidAndAttributes))).Sid).Value;}finally{Marshal.FreeHGlobal(data);}}finally{CloseHandle(token);}}
  public static string Acl(SafeHandle handle,int kind,string caller,bool pipe){
   IntPtr owner,group,dacl,sacl,descriptor;uint code=GetSecurityInfo(handle,kind,5,out owner,out group,out dacl,out sacl,out descriptor);
   if(code!=0||descriptor==IntPtr.Zero){if(descriptor!=IntPtr.Zero)LocalFree(descriptor);throw new InvalidOperationException("guest_com_acl_unknown");}try{
    uint size=GetSecurityDescriptorLength(descriptor);Rules.Require(size>0&&size<=65536,"guest_com_acl_unknown");byte[] raw=new byte[size];Marshal.Copy(descriptor,raw,0,raw.Length);var sd=new RawSecurityDescriptor(raw,0);
    Rules.Require(sd.Owner!=null&&Rules.TrustedSid(sd.Owner.Value,caller)&&sd.DiscretionaryAcl!=null&&sd.DiscretionaryAcl.Count>0,"guest_com_acl_owner_rejected");
    foreach(GenericAce ace in sd.DiscretionaryAcl){var item=ace as CommonAce;Rules.Require(item!=null&&!item.IsCallback,"guest_com_acl_type_rejected");if(item.AceQualifier==AceQualifier.AccessAllowed){uint mask=unchecked((uint)item.AccessMask);Rules.Require(pipe?Rules.AllowPipeAce(item.SecurityIdentifier.Value,mask,caller):Rules.TrustedSid(item.SecurityIdentifier.Value,caller)||mask==0,"guest_com_acl_write_rejected");}else Rules.Require(item.AceQualifier==AceQualifier.AccessDenied,"guest_com_acl_type_rejected");}
    return Rules.Hash(Encoding.UTF8.GetBytes(sd.GetSddlForm(AccessControlSections.Owner|AccessControlSections.Access)));
   }finally{LocalFree(descriptor);}
  }
  public static uint PipePid(SafePipeHandle handle){uint pid;Rules.Require(GetNamedPipeServerProcessId(handle,out pid)&&pid>0,"guest_com_pipe_pid_unknown");return pid;}
  [StructLayout(LayoutKind.Sequential)]public struct FileInfo{public uint Attributes;public Time Creation;public Time Access;public Time Write;public uint Volume;public uint SizeHigh;public uint SizeLow;public uint Links;public uint IndexHigh;public uint IndexLow;}
  [DllImport("kernel32.dll",SetLastError=true)]static extern bool GetFileInformationByHandle(SafeFileHandle handle,out FileInfo info);
  [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)]static extern SafeFileHandle CreateFile(string path,uint access,uint share,IntPtr security,uint disposition,uint flags,IntPtr template);
  [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)]static extern uint GetFinalPathNameByHandle(SafeFileHandle handle,StringBuilder path,uint size,uint flags);
  public static SafeFileHandle OpenPrivate(string path){var h=CreateFile(path,0x80020000U,1,IntPtr.Zero,3,0x00200000U,IntPtr.Zero);if(h.IsInvalid){h.Dispose();throw new InvalidOperationException("guest_com_private_open_failed");}return h;}
  public static string FileIdentity(SafeFileHandle handle,string expected){FileInfo info;Rules.Require(GetFileInformationByHandle(handle,out info)&&info.Links==1&&(info.Attributes&0x410U)==0,"guest_com_private_file_identity_rejected");var buffer=new StringBuilder(32768);uint size=GetFinalPathNameByHandle(handle,buffer,(uint)buffer.Capacity,0);Rules.Require(size>0&&size<buffer.Capacity,"guest_com_private_path_unknown");string path=buffer.ToString();if(path.StartsWith(@"\\?\",StringComparison.Ordinal))path=path.Substring(4);Rules.Require(String.Equals(Path.GetFullPath(path),Path.GetFullPath(expected),StringComparison.OrdinalIgnoreCase),"guest_com_private_path_changed");return info.Volume.ToString("x8")+":"+info.IndexHigh.ToString("x8")+info.IndexLow.ToString("x8");}
 }
 public sealed class PrivateFile:IDisposable {
  readonly FileStream stream;readonly string path,caller,hash,identity,acl;public readonly byte[] Bytes;
  public PrivateFile(string p,string expected,string who){path=p;caller=who;SafeFileHandle handle=Native.OpenPrivate(path);try{stream=new FileStream(handle,FileAccess.Read,4096,false);identity=Native.FileIdentity(handle,path);acl=Native.Acl(handle,1,caller,false);Rules.Require(stream.Length>0&&stream.Length<=65536,"guest_com_private_size_rejected");Bytes=new byte[(int)stream.Length];int used=0;while(used<Bytes.Length){int count=stream.Read(Bytes,used,Bytes.Length-used);Rules.Require(count>0,"guest_com_private_read_failed");used+=count;}hash=Rules.Hash(Bytes);Rules.Require(hash==expected,"guest_com_private_hash_changed");}catch{if(stream!=null)stream.Dispose();else handle.Dispose();throw;}}
  public void AssertUnchanged(){Rules.Require(Native.FileIdentity(stream.SafeFileHandle,path)==identity&&Native.Acl(stream.SafeFileHandle,1,caller,false)==acl,"guest_com_private_identity_changed");stream.Position=0;using(var h=SHA256.Create())Rules.Require(BitConverter.ToString(h.ComputeHash(stream)).Replace("-","").ToLowerInvariant()==hash,"guest_com_private_hash_changed");}
  public void Dispose(){stream.Dispose();}
 }
 public sealed class ProcessLease:IDisposable {
  IntPtr handle;public readonly uint Pid;public readonly long StartTicks;readonly string expected;
  public ProcessLease(uint pid,string image){Pid=pid;expected=image;handle=Native.OpenProcess(0x00101000U,false,pid);Rules.Require(handle!=IntPtr.Zero,"guest_com_vmwp_query_denied");try{StartTicks=Start();AssertAlive();}catch{Dispose();throw;}}
  long Start(){Native.Time a,b,c,d;Rules.Require(Native.GetProcessTimes(handle,out a,out b,out c,out d),"guest_com_vmwp_time_unknown");return DateTime.FromFileTimeUtc(a.Value()).Ticks;}
  public void AssertAlive(){var path=new StringBuilder(32768);uint length=(uint)path.Capacity;Rules.Require(Native.QueryFullProcessImageName(handle,0,path,ref length),"guest_com_vmwp_image_unknown");Rules.ProcessBinding(Pid,Pid,Pid,StartTicks,Start(),Native.WaitForSingleObject(handle,0)==258,path.ToString(),expected,Native.ProcessOwner(handle));}
  public void Dispose(){if(handle!=IntPtr.Zero){Native.CloseHandle(handle);handle=IntPtr.Zero;}}
 }
 public sealed class Reader:IDisposable {
  readonly NamedPipeClientStream pipe;readonly byte[] buffer=new byte[4096];Task<int> pending;string acl;
  public Reader(string name,int timeout){pipe=new NamedPipeClientStream(".",name,PipeDirection.In,PipeOptions.Asynchronous,TokenImpersonationLevel.Anonymous);try{pipe.Connect(timeout);}catch{pipe.Dispose();throw;}}
  public static Reader TryConnect(string name,int timeout){try{return new Reader(name,timeout);}catch(TimeoutException){return null;}catch(IOException){return null;}}
  public void Verify(uint expected,string caller){Rules.Require(Native.PipePid(pipe.SafePipeHandle)==expected,"guest_com_pipe_server_mismatch");string current=Native.Acl(pipe.SafePipeHandle,6,caller,true);if(acl==null)acl=current;Rules.Require(acl==current,"guest_com_pipe_acl_changed");}
  public byte[] Read(int timeout){if(pending==null)pending=pipe.ReadAsync(buffer,0,buffer.Length);if(Task.WaitAny(new Task[]{pending},timeout)<0)return null;int size;try{size=pending.GetAwaiter().GetResult();}catch(IOException){pending=null;return new byte[0];}catch(ObjectDisposedException){pending=null;return new byte[0];}pending=null;if(size==0)return new byte[0];byte[] result=new byte[size];Buffer.BlockCopy(buffer,0,result,0,size);return result;}
  public void Dispose(){pipe.Dispose();if(pending!=null){try{pending.Wait(100);}catch{}pending=null;}}
 }
#endif
}
'@
function Initialize-GuestComTypes([switch]$PortableFixture){
    if($script:GuestComInitialized){return}
    if('HaGuestComV1.Rules' -as [type]){throw 'guest_com_type_already_loaded'}
    if($PortableFixture){Add-Type -TypeDefinition $script:GuestComSource -Language CSharp -CompilerOptions '/define:PORTABLE_FIXTURE'}
    elseif([Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT -and $PSVersionTable.PSEdition -eq 'Desktop'){
        # Windows PowerShell does not implicitly reference System.Core (Pipes).
        Add-Type -TypeDefinition $script:GuestComSource -Language CSharp -ReferencedAssemblies @('System.dll','System.Core.dll')
    }else{Add-Type -TypeDefinition $script:GuestComSource -Language CSharp}
    $script:GuestComInitialized=$true
}
function Get-GuestComHash([byte[]]$Bytes){$h=[Security.Cryptography.SHA256]::Create();try{return [BitConverter]::ToString($h.ComputeHash($Bytes)).Replace('-','').ToLowerInvariant()}finally{$h.Dispose()}}
function Read-GuestComRequest{
    if($RequestSha256 -cnotmatch '^[0-9a-f]{64}$' -or $Request.Length -gt 32768){throw 'guest_com_request_pin_required'}
    $raw=[Convert]::FromBase64String($Request)
    if((Get-GuestComHash $raw) -cne $RequestSha256){throw 'guest_com_request_hash_changed'}
    $value=[HaGuestComV1.Json]::Object(([Text.UTF8Encoding]::new($false,$true)).GetString($raw),16384)
    [HaGuestComV1.Rules]::Keys($value,@('schema_version','collector_source_commit','vm_source_commit','stage_source_commit','seed_source_commit','installation_id','node','vm_id','prepared_receipt_sha256','seed_id','bootstrap_manifest_sha256','expected_kernel_release','expected_pg_image','expected_package_count','timeout_seconds','egress_mac_address','peer_prepared_receipt_sha256','switch_owner_manifest_sha256'))
    if([HaGuestComV1.Rules]::Integer($value,'schema_version') -ne 1){throw 'guest_com_request_version_rejected'}
    foreach($key in @('collector_source_commit','vm_source_commit','stage_source_commit','seed_source_commit')){if(-not [HaGuestComV1.Rules]::Match([HaGuestComV1.Rules]::Text($value,$key),'[0-9a-f]{40}')){throw 'guest_com_source_pin_rejected'}}
    foreach($key in @('installation_id','prepared_receipt_sha256','seed_id','bootstrap_manifest_sha256','switch_owner_manifest_sha256')){if(-not [HaGuestComV1.Rules]::Match([HaGuestComV1.Rules]::Text($value,$key),'[0-9a-f]{64}')){throw 'guest_com_identity_pin_rejected'}}
    if([HaGuestComV1.Rules]::Text($value,'node') -cnotin @('A','B') -or -not [HaGuestComV1.Rules]::Match([HaGuestComV1.Rules]::Text($value,'vm_id'),'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}')){throw 'guest_com_vm_pin_rejected'}
    $egress=[HaGuestComV1.Rules]::Text($value,'egress_mac_address');$peer=[HaGuestComV1.Rules]::Text($value,'peer_prepared_receipt_sha256')
    if(($egress -cne '' -and $egress -cnotmatch '^[0-9A-F]{12}$') -or ($value['node'] -ceq 'A' -and $peer -cne '') -or ($value['node'] -ceq 'B' -and $peer -cnotmatch '^[0-9a-f]{64}$')){throw 'guest_com_topology_pin_rejected'}
    if([HaGuestComV1.Rules]::Text($value,'expected_kernel_release') -cne '6.8.0-149-generic' -or -not [HaGuestComV1.Rules]::Match([HaGuestComV1.Rules]::Text($value,'expected_pg_image'),'sha256:[0-9a-f]{64}') -or [HaGuestComV1.Rules]::Integer($value,'expected_package_count') -ne 73){throw 'guest_com_runtime_pin_rejected'}
    if([HaGuestComV1.Rules]::Integer($value,'timeout_seconds') -lt 10 -or [HaGuestComV1.Rules]::Integer($value,'timeout_seconds') -gt 3600){throw 'guest_com_timeout_rejected'}
    return ,$value
}
function Assert-GuestComAncestors([string]$Path,[string]$Root,[string]$Caller){
    $current=[IO.Path]::GetFullPath($Path);$stop=[IO.Path]::GetFullPath($Root)
    while($true){
        $entry=Get-Item -LiteralPath $current -Force
        if($entry.Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'guest_com_private_reparse_rejected'}
        $sd=[Security.AccessControl.RawSecurityDescriptor]::new((Get-Acl -LiteralPath $current).Sddl)
        if(-not [HaGuestComV1.Rules]::TrustedSid($sd.Owner.Value,$Caller) -or $null -eq $sd.DiscretionaryAcl -or $sd.DiscretionaryAcl.Count -eq 0){throw 'guest_com_private_owner_rejected'}
        foreach($ace in $sd.DiscretionaryAcl){
            if($ace -isnot [Security.AccessControl.CommonAce] -or $ace.IsCallback){throw 'guest_com_private_acl_rejected'}
            if($ace.AceQualifier -eq [Security.AccessControl.AceQualifier]::AccessAllowed -and $ace.AccessMask -ne 0 -and -not [HaGuestComV1.Rules]::TrustedSid($ace.SecurityIdentifier.Value,$Caller)){throw 'guest_com_private_acl_rejected'}
        }
        if($current -ieq $stop){break}
        $next=[IO.Path]::GetDirectoryName($current)
        if(-not $next -or $next.Length -lt $stop.Length){throw 'guest_com_private_root_escape'}
        $current=$next
    }
}
function Read-GuestComPrepared($Lease,$Spec,[string]$Root){
    $value=[HaGuestComV1.Json]::Object(([Text.UTF8Encoding]::new($false,$true)).GetString($Lease.Bytes),65536)
    [HaGuestComV1.Rules]::Keys($value,@('schema_version','prepared','pins'))
    if([HaGuestComV1.Rules]::Integer($value,'schema_version') -ne 1 -or -not [HaGuestComV1.Rules]::Boolean($value,'prepared')){throw 'guest_com_prepared_version_rejected'}
    $pins=$value['pins']
    if($pins -isnot [Collections.Generic.Dictionary[string,object]]){throw 'guest_com_prepared_pins_rejected'}
    [HaGuestComV1.Rules]::Keys($pins,@('installation_id','source_commit','stage_source_commit','seed_source_commit','node','manifest_sha256','stage_manifest_sha256','vm_id','vm_name','switch_id','switch_name','config_path','disk_path','disk_sha256','disk_identity','disk_identifier','seed_path','seed_sha256','seed_identity','mac_address','com1_pipe','optional_egress_nic','egress_mac_address','peer_prepared_receipt_sha256','switch_owner_manifest_sha256'))
    foreach($key in @('egress_mac_address','peer_prepared_receipt_sha256','switch_owner_manifest_sha256')){if($pins[$key] -cne $Spec[$key]){throw 'guest_com_prepared_topology_mismatch'}}
    if($pins['optional_egress_nic'] -isnot [bool]){throw 'guest_com_prepared_type_rejected'}
    foreach($key in @('vm_name','switch_id','switch_name','config_path','disk_path','disk_identity','disk_identifier','seed_path','seed_identity','mac_address','com1_pipe')){if($pins[$key] -isnot [string] -or -not $pins[$key] -or $pins[$key].Length -gt 1024){throw 'guest_com_prepared_type_rejected'}}
    if(($pins['optional_egress_nic'] -and ($pins['egress_mac_address'] -cnotmatch '^[0-9A-F]{12}$' -or $pins['egress_mac_address'] -ceq $pins['mac_address'])) -or (-not $pins['optional_egress_nic'] -and $pins['egress_mac_address'] -cne '')){throw 'guest_com_prepared_topology_rejected'}
    if($pins['mac_address'] -cnotmatch '^[0-9A-F]{12}$'){throw 'guest_com_prepared_mac_rejected'}
    $vmName='N100HA_'+$Spec['installation_id'].Substring(0,16)+'_'+$Spec['node']
    $guestRoot=$Root+'\guest-'+$Spec['node']
    if($pins['vm_name'] -cne $vmName -or $pins['switch_name'] -cne ('N100HA_'+$Spec['installation_id'].Substring(0,16)+'_data') -or
        $pins['disk_path'] -ine ($guestRoot+'\guest-'+$Spec['node']+'.vhdx') -or $pins['seed_path'] -ine ($Root+'\seed-'+$Spec['node']+'.iso') -or
        $pins['config_path'] -inotmatch ('^'+[regex]::Escape($guestRoot)+'(?:'+[regex]::Escape('\'+$vmName)+')?$')){throw 'guest_com_prepared_path_rejected'}
    foreach($key in @('installation_id','node','vm_id','stage_source_commit','seed_source_commit')){if($pins[$key] -cne $Spec[$key]){throw 'guest_com_prepared_identity_mismatch'}}
    if($pins['source_commit'] -cne $Spec['vm_source_commit']){throw 'guest_com_prepared_source_mismatch'}
    foreach($key in @('manifest_sha256','stage_manifest_sha256','disk_sha256','seed_sha256')){if($pins[$key] -isnot [string] -or $pins[$key] -cnotmatch '^[0-9a-f]{64}$'){throw 'guest_com_prepared_hash_rejected'}}
    if($Spec['node'] -ceq 'A' -and $pins['switch_owner_manifest_sha256'] -cne $pins['manifest_sha256']){throw 'guest_com_prepared_switch_owner_rejected'}
    $expected='\\.\pipe\N100HA_'+$Spec['installation_id'].Substring(0,16)+'_'+$Spec['node']+'_com1'
    if($pins['com1_pipe'] -cne $expected){throw 'guest_com_prepared_pipe_rejected'}
    return ,$pins
}
function Get-GuestComVm($Spec,$Pins){
    $vm=Get-VM -Id ([Guid]$Spec['vm_id']) -ErrorAction Stop
    if($vm.Id.Guid -ine $Spec['vm_id'] -or $vm.Generation -ne 2 -or $vm.Name -cne $Pins['vm_name'] -or $vm.Notes -cne ('ha-owned-guest-v1:'+$Spec['installation_id']+':'+$Pins['manifest_sha256'])){throw 'guest_com_current_vm_identity_mismatch'}
    if($vm.Path -ine $Pins['config_path']){throw 'guest_com_current_config_path_changed'}
    $allPorts=@(Get-VMComPort -VM $vm);$ports=@($allPorts|Where-Object{$_.Number -eq 1})
    if($ports.Count -ne 1 -or $ports[0].Path -cne $Pins['com1_pipe'] -or @($allPorts|Where-Object{$_.Number -ne 1 -and $_.Path}).Count -ne 0){throw 'guest_com_current_com_path_changed'}
    $switch=Get-VMSwitch -Id ([Guid]$Pins['switch_id'])
    if($switch.Id.Guid -ine $Pins['switch_id'] -or $switch.Name -cne $Pins['switch_name'] -or [string]$switch.SwitchType -cne 'Internal' -or $switch.Notes -cne ('ha-owned-guest-v1:'+$Spec['installation_id']+':'+$Pins['switch_owner_manifest_sha256'])){throw 'guest_com_current_switch_mismatch'}
    $nics=@(Get-VMNetworkAdapter -VM $vm);$ha=@($nics|Where-Object{$_.Name -ceq 'ha-data'})
    if($ha.Count -ne 1 -or $ha[0].MacAddress -cne $Pins['mac_address'] -or $ha[0].DynamicMacAddressEnabled -ne $false -or [string]$ha[0].MacAddressSpoofing -cne 'Off' -or $ha[0].SwitchId -ine $Pins['switch_id']){throw 'guest_com_current_ha_nic_mismatch'}
    if($Pins['optional_egress_nic']){
        $egress=@($nics|Where-Object{$_.Name -ceq 'egress-disconnected'})
        if($nics.Count -ne 2 -or $egress.Count -ne 1 -or $egress[0].MacAddress -cne $Pins['egress_mac_address'] -or $egress[0].DynamicMacAddressEnabled -ne $false -or [string]$egress[0].MacAddressSpoofing -cne 'Off' -or $egress[0].SwitchName -notin @($null,'') -or [string]$egress[0].SwitchId -notin @('','00000000-0000-0000-0000-000000000000')){throw 'guest_com_egress_not_disconnected'}
    }elseif($nics.Count -ne 1){throw 'guest_com_unexpected_guest_nic'}
    $disks=@(Get-VMHardDiskDrive -VM $vm);$dvds=@(Get-VMDvdDrive -VM $vm)
    if($disks.Count -ne 1 -or $disks[0].Path -ine $Pins['disk_path'] -or $dvds.Count -ne 1 -or $dvds[0].Path -ine $Pins['seed_path']){throw 'guest_com_current_media_changed'}
    $systems=@(Get-CimInstance -Namespace 'root\virtualization\v2' -ClassName Msvm_ComputerSystem -Filter ("Name='"+$Spec['vm_id']+"'") -OperationTimeoutSec 3)
    if($systems.Count -ne 1 -or [string]$systems[0].Name -ine $Spec['vm_id']){throw 'guest_com_vm_provider_unknown'}
    $running=([string]$vm.State -ceq 'Running' -and [int]$systems[0].EnabledState -eq 2)
    if(-not $running -and [string]$vm.State -cnotin @('Off','Starting')){throw 'guest_com_vm_state_rejected'}
    return [pscustomobject]@{State=[string]$vm.State;Running=$running;Pid=[uint32]$systems[0].ProcessID}
}
function New-GuestComReaderReady($Spec,$Pins,[string]$Root,[string]$Caller){
    $path=Join-Path $Root ('com-'+$Spec['node']+'-reader-ready.json')
    if(Test-Path -LiteralPath $path){throw 'guest_com_reader_ready_exists'}
    $current=[Diagnostics.Process]::GetCurrentProcess()
    try{
        $expected=Join-Path ([Environment]::GetFolderPath([Environment+SpecialFolder]::System)) 'WindowsPowerShell\v1.0\powershell.exe'
        if($current.MainModule.FileName -ine $expected -or $current.HasExited){throw 'guest_com_system_powershell_required'}
        $ready=[ordered]@{format='ha-guest-com-reader-ready-v1';schema_version=1;source_commit=$Spec['collector_source_commit'];request_sha256=$RequestSha256;prepared_receipt_sha256=$Spec['prepared_receipt_sha256'];installation_id=$Spec['installation_id'];node=$Spec['node'];vm_id=$Spec['vm_id'];com1_pipe=$Pins['com1_pipe'];reader_pid=$current.Id;reader_start_ticks=$current.StartTime.ToUniversalTime().Ticks;reader_image_sha256=(Get-GuestComHash ([IO.File]::ReadAllBytes($expected)));pipe_bound=$false}
        $raw=[Text.UTF8Encoding]::new($false,$true).GetBytes(($ready|ConvertTo-Json -Depth 3 -Compress))
        $file=[IO.File]::Open($path,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
        try{$file.Write($raw,0,$raw.Length);$file.Flush($true)}finally{$file.Dispose()}
        Assert-GuestComAncestors $path $Root $Caller
        return Get-GuestComHash $raw
    }finally{$current.Dispose()}
}
function Invoke-GuestComReceipt{
    if(-not $Go -or [Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT -or -not [Environment]::Is64BitProcess){throw 'guest_com_native_intent_required'}
    $identity=[Security.Principal.WindowsIdentity]::GetCurrent()
    try{
        if(-not ([Security.Principal.WindowsPrincipal]::new($identity)).IsInRole([Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))){throw 'guest_com_native_admin_required'}
        $caller=$identity.User.Value
    }finally{$identity.Dispose()}
    Initialize-GuestComTypes
    $spec=Read-GuestComRequest
    $parent=[Environment]::GetFolderPath([Environment+SpecialFolder]::CommonApplicationData)
    if((Get-Item -LiteralPath $parent -Force).Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'guest_com_parent_reparse_rejected'}
    $root=Join-Path $parent ('N100HA_'+$spec['installation_id'].Substring(0,24))
    $path=Join-Path $root ('vm-'+$spec['node']+'-prepared-receipt.json')
    Assert-GuestComAncestors $path $root $caller
    $file=$null;$process=$null;$reader=$null
    try{
        $file=[HaGuestComV1.PrivateFile]::new($path,$spec['prepared_receipt_sha256'],$caller)
        $pins=Read-GuestComPrepared $file $spec $root
        $initial=Get-GuestComVm $spec $pins
        if($initial.State -cne 'Off' -or $initial.Pid -ne 0){throw 'guest_com_reader_requires_fresh_off_vm'}
        $readyHash=New-GuestComReaderReady $spec $pins $root $caller
        $frame=[HaGuestComV1.Framer]::new();$clock=[Diagnostics.Stopwatch]::StartNew();$connections=0
        while($clock.Elapsed.TotalSeconds -lt $spec['timeout_seconds']){
            $file.AssertUnchanged();$current=Get-GuestComVm $spec $pins
            if(-not $current.Running){if($null -ne $process){throw 'guest_com_vm_stopped_after_connection'};Start-Sleep -Milliseconds 200;continue}
            if($current.Pid -eq 0){if($null -ne $process){throw 'guest_com_vmwp_unknown'};Start-Sleep -Milliseconds 200;continue}
            if($null -eq $process){$process=[HaGuestComV1.ProcessLease]::new($current.Pid,(Join-Path ([Environment]::GetFolderPath([Environment+SpecialFolder]::System)) 'vmwp.exe'))}
            if($process.Pid -ne $current.Pid){throw 'guest_com_vmwp_replaced'}
            $process.AssertAlive()
            if($null -eq $reader){
                if($connections -ge 2){throw 'guest_com_reconnect_limit'}
                $reader=[HaGuestComV1.Reader]::TryConnect($pins['com1_pipe'].Substring(9),500)
                if($null -eq $reader){continue}
                $connections++;$frame.ResetConnection();$reader.Verify($process.Pid,$caller)
            }
            $chunk=$null
            try{$chunk=$reader.Read(500)}catch [IO.IOException]{$reader.Dispose();$reader=$null;continue}
            if($null -eq $chunk){continue}
            if($chunk.Length -eq 0){$reader.Dispose();$reader=$null;continue}
            $payload=$frame.Feed($chunk,$chunk.Length)
            if($null -eq $payload){continue}
            $guest=[HaGuestComV1.Rules]::Receipt($payload,$spec)
            $reader.Verify($process.Pid,$caller);$process.AssertAlive();$after=Get-GuestComVm $spec $pins
            if(-not $after.Running -or $after.Pid -ne $process.Pid){throw 'guest_com_vmwp_changed_after_receipt'}
            $file.AssertUnchanged();Assert-GuestComAncestors $path $root $caller
            if($clock.Elapsed.TotalSeconds -ge $spec['timeout_seconds']){throw 'guest_com_timeout_unknown'}
            if($guest['status'] -cne 'PASS'){throw 'guest_com_guest_reported_failure'}
            return [ordered]@{status='PASS';scope='owned_guest_retained_com_public_key_receipt';collector_source_commit=$spec['collector_source_commit'];reader_ready_sha256=$readyHash;vm_source_commit=$spec['vm_source_commit'];stage_source_commit=$spec['stage_source_commit'];seed_source_commit=$spec['seed_source_commit'];installation_id=$spec['installation_id'];node=$spec['node'];vm_id=$spec['vm_id'];prepared_receipt_sha256=$spec['prepared_receipt_sha256'];vmwp_pid=$process.Pid;vmwp_start_ticks=$process.StartTicks;connections=$connections;bytes_read=$frame.Bytes;lines_seen=$frame.Lines;native_host_boundary_verified=$true;pipe_server_pid_verified=$true;pipe_acl_verified=$true;current_vm_com_path_verified=$true;guest_receipt=$guest;windows_fence_verified=$false;writer_authority_issued=$false;production_transition=$false}
        }
        throw 'guest_com_timeout_unknown'
    }finally{if($null -ne $reader){$reader.Dispose()};if($null -ne $process){$process.Dispose()};if($null -ne $file){$file.Dispose()}}
}
if($MyInvocation.InvocationName -ne '.'){
    try{Invoke-GuestComReceipt|ConvertTo-Json -Depth 8 -Compress}catch{
        $reason='guest_com_unknown';$message=$_.Exception.GetBaseException().Message
        if($message -cmatch '^guest_com_[a-z0-9_]{1,100}$'){$reason=$message}
        [ordered]@{status='UNKNOWN';reason=$reason;native_host_boundary_verified=$false;production_transition=$false}|ConvertTo-Json -Compress
        exit 1
    }
}
