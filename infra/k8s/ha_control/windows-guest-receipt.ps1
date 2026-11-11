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
$script:guestOwnedSpec=$null;$script:guestOwnedRoot=''
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
  public static bool VirtualMachineSid(string sid){if(!Match(sid,"S-1-5-83-1(?:-(?:0|[1-9][0-9]{0,9})){4}"))return false;string[] parts=sid.Split('-');bool nonzero=false;for(int i=5;i<parts.Length;i++){uint value;if(!UInt32.TryParse(parts[i],out value))return false;nonzero|=value!=0;}return nonzero;}
  public static bool TrustedPipeSid(string sid,string caller,string vmSid){return TrustedSid(sid,caller)||(VirtualMachineSid(vmSid)&&sid==vmSid);}
  public static bool AllowPipeAce(string sid,uint mask,string caller,string vmSid){const uint read=0x80000000U|0x00100000U|0x00020000U|0x00000001U|0x00000008U|0x00000080U;return TrustedPipeSid(sid,caller,vmSid)||(mask&~read)==0;}
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
  public static void ProcessBinding(uint vmPid,uint serverPid,uint retainedPid,long before,long after,bool alive,string image,string expected,string owner,string expectedVmSid){Require(vmPid>0&&vmPid==serverPid&&vmPid==retainedPid&&before>0&&before==after&&alive&&String.Equals(image,expected,StringComparison.OrdinalIgnoreCase)&&VirtualMachineSid(expectedVmSid)&&owner==expectedVmSid,"guest_com_vmwp_binding_rejected");}
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
  public static string Acl(SafeHandle handle,int kind,string caller,bool pipe,string vmSid=""){
   IntPtr owner,group,dacl,sacl,descriptor;uint code=GetSecurityInfo(handle,kind,5,out owner,out group,out dacl,out sacl,out descriptor);
   if(code!=0||descriptor==IntPtr.Zero){if(descriptor!=IntPtr.Zero)LocalFree(descriptor);throw new InvalidOperationException("guest_com_acl_unknown");}try{
    uint size=GetSecurityDescriptorLength(descriptor);Rules.Require(size>0&&size<=65536,"guest_com_acl_unknown");byte[] raw=new byte[size];Marshal.Copy(descriptor,raw,0,raw.Length);var sd=new RawSecurityDescriptor(raw,0);
    Rules.Require(sd.Owner!=null&&(pipe?Rules.TrustedPipeSid(sd.Owner.Value,caller,vmSid):Rules.TrustedSid(sd.Owner.Value,caller))&&sd.DiscretionaryAcl!=null&&sd.DiscretionaryAcl.Count>0,"guest_com_acl_owner_rejected");
    foreach(GenericAce ace in sd.DiscretionaryAcl){var item=ace as CommonAce;Rules.Require(item!=null&&!item.IsCallback,"guest_com_acl_type_rejected");if(item.AceQualifier==AceQualifier.AccessAllowed){uint mask=unchecked((uint)item.AccessMask);Rules.Require(pipe?Rules.AllowPipeAce(item.SecurityIdentifier.Value,mask,caller,vmSid):Rules.TrustedSid(item.SecurityIdentifier.Value,caller)||mask==0,"guest_com_acl_write_rejected");}else Rules.Require(item.AceQualifier==AceQualifier.AccessDenied,"guest_com_acl_type_rejected");}
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
  IntPtr handle;public readonly uint Pid;public readonly long StartTicks;readonly string expected,vmSid;
  public ProcessLease(uint pid,string image,string expectedVmSid){Rules.Require(Rules.VirtualMachineSid(expectedVmSid),"guest_com_vm_sid_rejected");Pid=pid;expected=image;vmSid=expectedVmSid;handle=Native.OpenProcess(0x00101000U,false,pid);Rules.Require(handle!=IntPtr.Zero,"guest_com_vmwp_query_denied");try{StartTicks=Start();AssertAlive();}catch{Dispose();throw;}}
  long Start(){Native.Time a,b,c,d;Rules.Require(Native.GetProcessTimes(handle,out a,out b,out c,out d),"guest_com_vmwp_time_unknown");return DateTime.FromFileTimeUtc(a.Value()).Ticks;}
  public void AssertAlive(){var path=new StringBuilder(32768);uint length=(uint)path.Capacity;Rules.Require(Native.QueryFullProcessImageName(handle,0,path,ref length),"guest_com_vmwp_image_unknown");Rules.ProcessBinding(Pid,Pid,Pid,StartTicks,Start(),Native.WaitForSingleObject(handle,0)==258,path.ToString(),expected,Native.ProcessOwner(handle),vmSid);}
  public void Dispose(){if(handle!=IntPtr.Zero){Native.CloseHandle(handle);handle=IntPtr.Zero;}}
 }
 public sealed class Reader:IDisposable {
  readonly NamedPipeClientStream pipe;readonly byte[] buffer=new byte[4096];Task<int> pending;string acl;
  public Reader(string name,int timeout){pipe=new NamedPipeClientStream(".",name,PipeDirection.In,PipeOptions.Asynchronous,TokenImpersonationLevel.Anonymous);try{pipe.Connect(timeout);}catch{pipe.Dispose();throw;}}
  public static Reader TryConnect(string name,int timeout){try{return new Reader(name,timeout);}catch(TimeoutException){return null;}catch(IOException){return null;}}
  public void Verify(uint expected,string caller,string vmSid){Rules.Require(Rules.VirtualMachineSid(vmSid)&&Native.PipePid(pipe.SafePipeHandle)==expected,"guest_com_pipe_server_mismatch");string current=Native.Acl(pipe.SafePipeHandle,6,caller,true,vmSid);if(acl==null)acl=current;Rules.Require(acl==current,"guest_com_pipe_acl_changed");}
  public void VerifyRegistered(uint expected,string expectedDescriptor){Rules.Require(Native.PipePid(pipe.SafePipeHandle)==expected,"guest_com_host_pipe_server_mismatch");string current=Rules.Hash(Encoding.UTF8.GetBytes(pipe.GetAccessControl().GetSecurityDescriptorSddlForm(AccessControlSections.Owner|AccessControlSections.Access)));Rules.Require(current==expectedDescriptor,"guest_com_host_pipe_descriptor_rejected");if(acl==null)acl=current;Rules.Require(acl==current,"guest_com_pipe_acl_changed");}
  public byte[] Read(int timeout){if(pending==null)pending=pipe.ReadAsync(buffer,0,buffer.Length);if(Task.WaitAny(new Task[]{pending},timeout)<0)return null;int size;try{size=pending.GetAwaiter().GetResult();}catch(IOException){pending=null;return new byte[0];}catch(ObjectDisposedException){pending=null;return new byte[0];}pending=null;if(size==0)return new byte[0];byte[] result=new byte[size];Buffer.BlockCopy(buffer,0,result,0,size);return result;}
  public void Dispose(){pipe.Dispose();if(pending!=null){try{pending.Wait(100);}catch{}pending=null;}}
 }
#endif
 public static class HostProfile {
  public const string Name="registered-host-compute-v1";
  public static Dictionary<string,object> Parse(string text,Dictionary<string,object> request){
   var p=Json.Object(text,16384);Rules.Keys(p,new string[]{"schema_version","profile_name","collector_source_commit","vm_source_commit","stage_source_commit","seed_source_commit","installation_id","node","vm_id","prepared_receipt_sha256","seed_id","evidence_sha256","evidence_source_sha256","expected_service","expected_pid","expected_start_ticks","expected_file_identity","expected_image_sha256","expected_signer_certificate_sha256","expected_service_sddl_sha256","expected_file_sddl_sha256","expected_pipe_sddl_sha256","capability_name","capability_sid_sha256","trust_model","scope","isolated_predeployment","writer_authority_issued","production_transition"});
   Rules.Require(Rules.Integer(p,"schema_version")==1&&Rules.Text(p,"profile_name")==Name&&Rules.Text(p,"expected_service")=="vmcompute"&&Rules.Text(p,"capability_name")=="vmWorkerProcess"&&Rules.Text(p,"capability_sid_sha256")=="59217592fcdabf0b9b76ab2987b0dce343b526fb9639c8e749d269f4e99797ce","guest_com_host_profile_rejected");
   foreach(string key in new string[]{"collector_source_commit","vm_source_commit","stage_source_commit","seed_source_commit","installation_id","node","vm_id","prepared_receipt_sha256","seed_id"})Rules.Require(Rules.Text(p,key)==Rules.Text(request,key),"guest_com_host_profile_identity_rejected");
   foreach(string key in new string[]{"evidence_sha256","evidence_source_sha256","expected_image_sha256","expected_signer_certificate_sha256","expected_service_sddl_sha256","expected_file_sddl_sha256","expected_pipe_sddl_sha256"})Rules.Require(Rules.Match(Rules.Text(p,key),"[0-9a-f]{64}")&&Rules.Text(p,key)!=new string('0',64),"guest_com_host_profile_pin_rejected");
   Rules.Require(Rules.Text(p,"evidence_sha256")==Rules.Text(request,"host_evidence_sha256")&&Rules.Text(p,"evidence_source_sha256")=="cea85b2ae3cf57ef1891faf08f8cde5d2daa25fbfd8e9d86e90ae33c7ced1ab8","guest_com_host_evidence_pin_rejected");
   Rules.Require(Rules.Integer(p,"expected_pid")>0&&Rules.Integer(p,"expected_pid")<=Int32.MaxValue&&Rules.Match(Rules.Text(p,"expected_start_ticks"),"[0-9]{15,20}")&&Rules.Match(Rules.Text(p,"expected_file_identity"),"[0-9a-f]{8}:[0-9a-f]{16}"),"guest_com_host_process_pin_rejected");
   Rules.Require(Rules.Text(p,"trust_model")=="HostHypervisorTCB"&&Rules.Text(p,"scope")=="isolated_predeployment_guest_enrollment"&&Rules.Boolean(p,"isolated_predeployment")&&!Rules.Boolean(p,"writer_authority_issued")&&!Rules.Boolean(p,"production_transition"),"guest_com_host_profile_scope_rejected");return p;
  }
  public static bool SafeControlAcl(Dictionary<string,object> acl,bool file){
   string owner=acl["owner_role"] as string;if(owner!="system"&&owner!="administrators"&&(!file||owner!="trusted_installer_os_file_only"))return false;
   var rows=acl["aces"] as List<object>;if(rows==null||rows.Count==0||rows.Count>128)return false;foreach(object row in rows){var a=row as Dictionary<string,object>;if(a==null||!(a["common_noncallback"] is bool)||!(bool)a["common_noncallback"])return false;string role=a["sid_role"] as string;string[] roles=file?new string[]{"system","administrators","builtin_users","trusted_installer_os_file_only","all_app_packages","all_restricted_app_packages"}:new string[]{"system","administrators","authenticated_users"};if(role==null||Array.IndexOf(roles,role)<0||((string)a["qualifier"]!="allow"&&(string)a["qualifier"]!="deny"))return false;if((bool)a["mutating_access"]&&role!="system"&&role!="administrators"&&(!file||role!="trusted_installer_os_file_only"))return false;}return true;
  }
  public static bool ServiceBinding(Dictionary<string,object> fact,Dictionary<string,object> p){
   var acl=fact["service_acl"] as Dictionary<string,object>;return acl!=null&&SafeControlAcl(acl,false)&&(uint)fact["service_type"]==16&&(uint)fact["service_pid"]==(uint)Rules.Integer(p,"expected_pid")&&(uint)fact["service_state"]==4&&(bool)fact["service_account_is_system"]&&(bool)fact["binary_exact_system32_vmcompute"]&&!(bool)acl["unclassified_writer_present"]&&(string)acl["sddl_sha256"]==Rules.Text(p,"expected_service_sddl_sha256");
  }
  public static bool ProcessBinding(Dictionary<string,object> f,Dictionary<string,object> p,string actualImage,string expectedImage){
   return f["server_pid"] is uint&&(uint)f["server_pid"]==(uint)Rules.Integer(p,"expected_pid")&&f["process_handle_retained"] is bool&&(bool)f["process_handle_retained"]&&f["process_identity_stable"] is bool&&(bool)f["process_identity_stable"]&&f["process_start_ticks"] is long&&((long)f["process_start_ticks"]).ToString(System.Globalization.CultureInfo.InvariantCulture)==Rules.Text(p,"expected_start_ticks")&&f["process_owner_role"] is string&&(string)f["process_owner_role"]=="system"&&String.Equals(actualImage,expectedImage,StringComparison.OrdinalIgnoreCase);
  }
  public static bool ModelAccepted(Dictionary<string,object> proof,string expectedHash){
   try{
   object v;if(!proof.TryGetValue("model_status",out v)||!(v is string)||(string)v!="OBSERVED"||!proof.ContainsKey("six_ace_descriptor_matches_observed")||!(proof["six_ace_descriptor_matches_observed"] is bool)||!(bool)proof["six_ace_descriptor_matches_observed"]||!proof.ContainsKey("reconstructed_pipe_sddl_sha256")||(string)proof["reconstructed_pipe_sddl_sha256"]!=expectedHash)return false;
   if(!(proof["capability_matches_observed"] is bool)||!(bool)proof["capability_matches_observed"]||!(proof["expected_vm_matches_observed"] is bool)||!(bool)proof["expected_vm_matches_observed"])return false;var cases=proof["cases"] as Dictionary<string,object>;if(cases==null||cases.Count!=8)return false;
   string[] names={"normal_unpriv_everyone","normal_foreign_vm","normal_own_vm","normal_hvg","app_unpriv_with_cap","app_foreign_vm_with_cap","app_own_vm_with_cap","app_hvg_without_cap"};bool[] allowed={false,false,true,true,false,false,true,false};
   for(int i=0;i<names.Length;i++){object row;if(!cases.TryGetValue(names[i],out row))return false;var r=row as Dictionary<string,object>;if(r==null||!r.ContainsKey("normal_and_capability_separated")||!(r["normal_and_capability_separated"] is bool)||!(bool)r["normal_and_capability_separated"]||!r.ContainsKey("model_status")||(string)r["model_status"]!="OBSERVED")return false;if(!(r["app_container_model"] is bool)||(bool)r["app_container_model"]!=(i>=4)||!(r["normal_group_contains_capability"] is bool)||(bool)r["normal_group_contains_capability"]||!(r["capability_list_contains_target"] is bool)||(bool)r["capability_list_contains_target"]!=(i>=4&&i!=7))return false;var checks=r["checks"] as System.Collections.Generic.List<object>;if(checks==null||checks.Count!=4)return false;uint[] masks={2,4,0x40000,0x80000};for(int j=0;j<4;j++){var c=checks[j] as Dictionary<string,object>;if(c==null||!(c["desired"] is uint)||(uint)c["desired"]!=masks[j]||!(c["api_succeeded"] is bool)||!(bool)c["api_succeeded"]||!(c["allowed"] is bool)||(bool)c["allowed"]!=allowed[i]||!(c["access_error"] is uint)||(uint)c["access_error"]!=(allowed[i]?0U:5U)||!(c["granted"] is uint)||((uint)c["granted"]&masks[j])!=(allowed[i]?masks[j]:0U)||(string)c["status"]!=(allowed[i]?"ALLOW":"DENY"))return false;}}
   return true;}catch{return false;}
  }
 }

 public static class MetadataRoles {
  public static string Sid(string value,string caller,string vm){
   if(value==vm)return "expected_vm";if(value==caller)return "caller";
   switch(value){case "S-1-5-18":return "system";case "S-1-5-32-544":return "administrators";case "S-1-1-0":return "everyone";case "S-1-5-7":return "anonymous";case "S-1-5-83-0":return "virtual_machines";case "S-1-5-32-578":return "hyperv_administrators";case "S-1-5-32-545":return "builtin_users";case "S-1-5-4":return "interactive";case "S-1-5-11":return "authenticated_users";case "S-1-5-6":return "service";case "S-1-5-19":return "local_service";case "S-1-5-20":return "network_service";case "S-1-5-12":return "restricted_code";case "S-1-5-33":return "write_restricted_code";case "S-1-3-0":return "creator_owner";case "S-1-3-1":return "creator_group";case "S-1-5-113":return "local_account";case "S-1-5-114":return "local_account_administrator";case "S-1-15-2-1":return "all_app_packages";case "S-1-15-2-2":return "all_restricted_app_packages";default:return "unclassified";}
  }
#if !PORTABLE_FIXTURE
  public static System.Collections.Generic.Dictionary<string,object> Pipe(System.IO.Pipes.NamedPipeClientStream pipe,string caller,string vm){
   var security=pipe.GetAccessControl();string sddl=security.GetSecurityDescriptorSddlForm(System.Security.AccessControl.AccessControlSections.Owner|System.Security.AccessControl.AccessControlSections.Access);
   var sd=new System.Security.AccessControl.RawSecurityDescriptor(sddl);Rules.Require(sd.Owner!=null&&sd.DiscretionaryAcl!=null&&sd.DiscretionaryAcl.Count<=128,"guest_com_acl_unknown");
   var result=new System.Collections.Generic.Dictionary<string,object>();result["owner_role"]=Sid(sd.Owner.Value,caller,vm);result["owner_sid_sha256"]=Rules.Hash(System.Text.Encoding.UTF8.GetBytes(sd.Owner.Value));result["sddl_sha256"]=Rules.Hash(System.Text.Encoding.UTF8.GetBytes(sddl));
   var rows=new System.Collections.Generic.List<object>();bool unknownWrite=false;
   foreach(System.Security.AccessControl.GenericAce item in sd.DiscretionaryAcl){
    var row=new System.Collections.Generic.Dictionary<string,object>();var common=item as System.Security.AccessControl.CommonAce;var known=item as System.Security.AccessControl.KnownAce;var qualified=item as System.Security.AccessControl.QualifiedAce;
    byte[] binary=new byte[item.BinaryLength];item.GetBinaryForm(binary,0);row["ace_sha256"]=Rules.Hash(binary);row["flags"]=(int)item.AceFlags;row["common_noncallback"]=common!=null&&!common.IsCallback;
    row["sid_role"]=known!=null?Sid(known.SecurityIdentifier.Value,caller,vm):"unavailable";row["sid_sha256"]=known!=null?Rules.Hash(System.Text.Encoding.UTF8.GetBytes(known.SecurityIdentifier.Value)):"";
    if(known!=null){uint mask=unchecked((uint)known.AccessMask);string role=(string)row["sid_role"];bool allow=qualified!=null&&qualified.AceQualifier==System.Security.AccessControl.AceQualifier.AccessAllowed;
     row["mask"]=mask;row["qualifier"]=allow?"allow":qualified!=null&&qualified.AceQualifier==System.Security.AccessControl.AceQualifier.AccessDenied?"deny":"other";row["create_instance_or_write_data"]=allow&&(mask&0x50000006U)!=0;row["write_dac_or_owner"]=allow&&(mask&0x100C0000U)!=0;
     if(role=="unclassified"&&allow&&(mask&0x500C0006U)!=0)unknownWrite=true;
    }
    if(common==null||common.IsCallback)unknownWrite=true;rows.Add(row);
   }
   result["aces"]=rows;result["unclassified_writer_present"]=unknownWrite;return result;
  }
#endif
 }
#if !PORTABLE_FIXTURE
 public sealed class MetadataProcessLease:System.IDisposable {
  System.IntPtr handle;readonly uint pid;long start;string image="",owner="";public bool Retained{get{return handle!=System.IntPtr.Zero;}}public uint OpenError{get;private set;}public uint QueryError{get;private set;}public string ImagePath{get{return image;}}
  public MetadataProcessLease(uint value){pid=value;handle=Native.OpenProcess(0x00101000U,false,pid);if(handle==System.IntPtr.Zero){OpenError=unchecked((uint)System.Runtime.InteropServices.Marshal.GetLastWin32Error());return;}try{start=Start();image=Image();}catch{QueryError=unchecked((uint)System.Runtime.InteropServices.Marshal.GetLastWin32Error());}try{owner=Native.ProcessOwner(handle);}catch{if(QueryError==0)QueryError=unchecked((uint)System.Runtime.InteropServices.Marshal.GetLastWin32Error());}}
  long Start(){Native.Time a,b,c,d;Rules.Require(Native.GetProcessTimes(handle,out a,out b,out c,out d),"guest_com_vmwp_time_unknown");return System.DateTime.FromFileTimeUtc(a.Value()).Ticks;}
  string Image(){var s=new System.Text.StringBuilder(32768);uint n=(uint)s.Capacity;Rules.Require(Native.QueryFullProcessImageName(handle,0,s,ref n),"guest_com_vmwp_image_unknown");return s.ToString();}
  public bool Stable(){if(!Retained||start<=0||image.Length==0||owner.Length==0)return false;try{return Native.WaitForSingleObject(handle,0)==258&&Start()==start&&System.String.Equals(Image(),image,System.StringComparison.OrdinalIgnoreCase)&&Native.ProcessOwner(handle)==owner;}catch{return false;}}
  public System.Collections.Generic.Dictionary<string,object> Facts(string caller,string vm,string system,uint vmwp,uint vmms){var f=new System.Collections.Generic.Dictionary<string,object>();f["server_pid"]=pid;f["server_pid_is_4"]=pid==4;f["server_pid_matches_vmwp"]=pid==vmwp;f["server_pid_matches_vmms"]=pid==vmms;f["process_handle_retained"]=Retained;f["process_start_ticks"]=start;f["process_identity_stable"]=Stable();f["process_open_error"]=OpenError;f["process_query_error"]=QueryError;f["process_owner_role"]=owner.Length>0?MetadataRoles.Sid(owner,caller,vm):"unknown";f["process_owner_sid_sha256"]=owner.Length>0?Rules.Hash(System.Text.Encoding.UTF8.GetBytes(owner)):"";f["process_image_path_sha256"]=image.Length>0?Rules.Hash(System.Text.Encoding.UTF8.GetBytes(image)):"";f["direct_system32_leaf_path"]=image.Length>0&&System.String.Equals(System.IO.Path.GetDirectoryName(image),system,System.StringComparison.OrdinalIgnoreCase);f["kernel_creator_authenticated"]=false;f["loaded_image_file_authenticated"]=false;return f;}
  public void Dispose(){if(Retained){Native.CloseHandle(handle);handle=System.IntPtr.Zero;}}
 }
 public sealed class MetadataImageLease:System.IDisposable {
  readonly System.IO.FileStream file;readonly string path,identity,hash;
  [System.Runtime.InteropServices.StructLayout(System.Runtime.InteropServices.LayoutKind.Sequential)]struct Info{public uint Attr;public Native.Time Creation,Access,Write;public uint Volume,High,Low,Links,IndexHigh,IndexLow;}
  [System.Runtime.InteropServices.DllImport("kernel32.dll",SetLastError=true)]static extern bool GetFileInformationByHandle(Microsoft.Win32.SafeHandles.SafeFileHandle handle,out Info info);
  public MetadataImageLease(string p,string system){path=System.IO.Path.GetFullPath(p);Rules.Require(System.String.Equals(System.IO.Path.GetDirectoryName(path),system,System.StringComparison.OrdinalIgnoreCase)&&System.Text.RegularExpressions.Regex.IsMatch(System.IO.Path.GetFileName(path),"\\A[A-Za-z0-9_.-]{1,100}\\.exe\\z"),"guest_com_private_path_unknown");var entry=new System.IO.FileInfo(path);for(System.IO.FileSystemInfo x=entry;x!=null;x=x is System.IO.DirectoryInfo?((System.IO.DirectoryInfo)x).Parent:((System.IO.FileInfo)x).Directory)Rules.Require((x.Attributes&System.IO.FileAttributes.ReparsePoint)==0,"guest_com_private_path_unknown");file=new System.IO.FileStream(path,System.IO.FileMode.Open,System.IO.FileAccess.Read,System.IO.FileShare.Read);try{identity=Identity();hash=Hash();}catch{file.Dispose();throw;}}
  string Identity(){Info i;Rules.Require(GetFileInformationByHandle(file.SafeFileHandle,out i)&&i.Links>0&&(i.Attr&0x410U)==0,"guest_com_private_file_identity_rejected");return i.Volume.ToString("x8")+":"+i.IndexHigh.ToString("x8")+i.IndexLow.ToString("x8");}
  string Hash(){file.Position=0;using(var h=System.Security.Cryptography.SHA256.Create())return System.BitConverter.ToString(h.ComputeHash(file)).Replace("-","").ToLowerInvariant();}
  public bool IdentityStable(){return Identity()==identity;}
  public System.Security.AccessControl.RawSecurityDescriptor Descriptor(){return new System.Security.AccessControl.RawSecurityDescriptor(file.GetAccessControl().GetSecurityDescriptorSddlForm(System.Security.AccessControl.AccessControlSections.Owner|System.Security.AccessControl.AccessControlSections.Access));}
  public bool Stable(){return Identity()==identity&&Hash()==hash;}
  public string FileIdentity{get{return identity;}}public string Sha256{get{return hash;}}
  public void Dispose(){file.Dispose();}
 }
#endif

 public static class HcsPure {
  public static bool ControlMutator(uint mask,bool allow,bool file){return allow&&(mask&(file?0x500D0156U:0x700D0172U))!=0;}
  public static string HashSid(string sid){return Rules.Hash(System.Text.Encoding.UTF8.GetBytes(sid));}
  public static string ServiceSid(string name){using(var h=System.Security.Cryptography.SHA1.Create()){byte[] b=h.ComputeHash(System.Text.Encoding.Unicode.GetBytes(name.ToUpperInvariant()));var s="S-1-5-80";for(int i=0;i<5;i++)s+="-"+System.BitConverter.ToUInt32(b,i*4).ToString(System.Globalization.CultureInfo.InvariantCulture);return s;}}
  public static bool ImageCommand(string actual,string expected){return System.String.Equals(actual,expected,System.StringComparison.OrdinalIgnoreCase)||System.String.Equals(actual,"\""+expected+"\"",System.StringComparison.OrdinalIgnoreCase);}
  public static bool CapabilitySeparation(string[] groups,string[] caps,bool hvg,bool requested,string cap){return System.Array.IndexOf(groups,cap)<0&&(System.Array.IndexOf(caps,cap)>=0)==requested&&caps.Length==(requested?1:0)&&System.Array.IndexOf(groups,"S-1-1-0")>=0&&(System.Array.IndexOf(groups,"S-1-5-32-578")>=0)==hvg&&groups.Length==(hvg?2:1);}
  public static string FileRole(string sid){if(sid==ServiceSid("TrustedInstaller"))return "trusted_installer_os_file_only";return MetadataRoles.Sid(sid,"","");}
 }
#if !PORTABLE_FIXTURE
 public sealed class HcsServiceLease:System.IDisposable {
  [System.Runtime.InteropServices.DllImport("advapi32.dll",CharSet=System.Runtime.InteropServices.CharSet.Unicode,SetLastError=true)]static extern System.IntPtr OpenSCManager(string machine,string database,uint access);
  [System.Runtime.InteropServices.DllImport("advapi32.dll",CharSet=System.Runtime.InteropServices.CharSet.Unicode,SetLastError=true)]static extern System.IntPtr OpenService(System.IntPtr scm,string name,uint access);
  [System.Runtime.InteropServices.DllImport("advapi32.dll",SetLastError=true)]static extern bool CloseServiceHandle(System.IntPtr h);
  [System.Runtime.InteropServices.DllImport("advapi32.dll",CharSet=System.Runtime.InteropServices.CharSet.Unicode,SetLastError=true)]static extern bool QueryServiceConfig(System.IntPtr h,System.IntPtr buffer,uint size,out uint needed);
  [System.Runtime.InteropServices.DllImport("advapi32.dll",SetLastError=true)]static extern bool QueryServiceStatusEx(System.IntPtr h,int level,out Status status,uint size,out uint needed);
  [System.Runtime.InteropServices.DllImport("advapi32.dll",SetLastError=true)]static extern bool QueryServiceObjectSecurity(System.IntPtr h,uint info,byte[] buffer,uint size,out uint needed);
  [System.Runtime.InteropServices.StructLayout(System.Runtime.InteropServices.LayoutKind.Sequential)]struct Config {public uint Type,Start,Error;public System.IntPtr Binary,Group;public uint Tag;public System.IntPtr Dependencies,Account,Display;}
  [System.Runtime.InteropServices.StructLayout(System.Runtime.InteropServices.LayoutKind.Sequential)]struct Status {public uint Type,State,Controls,Win32Exit,SpecificExit,Checkpoint,WaitHint,Pid,Flags;}
  System.IntPtr manager,service;public string BinaryPath{get;private set;}
  public HcsServiceLease(){manager=OpenSCManager(null,null,1);Rules.Require(manager!=System.IntPtr.Zero,"hcs_scm_query_unknown");try{service=OpenService(manager,"vmcompute",0x20005);Rules.Require(service!=System.IntPtr.Zero,"hcs_service_query_unknown");}catch{Dispose();throw;}}
  public System.Collections.Generic.Dictionary<string,object> Query(string system){
   uint n;QueryServiceConfig(service,System.IntPtr.Zero,0,out n);Rules.Require(n>0&&n<=8192,"hcs_config_size_unknown");var p=System.Runtime.InteropServices.Marshal.AllocHGlobal((int)n);
   try{Rules.Require(QueryServiceConfig(service,p,n,out n),"hcs_config_query_unknown");var c=(Config)System.Runtime.InteropServices.Marshal.PtrToStructure(p,typeof(Config));BinaryPath=System.Runtime.InteropServices.Marshal.PtrToStringUni(c.Binary);string account=System.Runtime.InteropServices.Marshal.PtrToStringUni(c.Account);Status s;Rules.Require(QueryServiceStatusEx(service,0,out s,(uint)System.Runtime.InteropServices.Marshal.SizeOf(typeof(Status)),out n),"hcs_status_query_unknown");
    uint size;QueryServiceObjectSecurity(service,5,null,0,out size);Rules.Require(size>0&&size<=8192,"hcs_service_acl_size_unknown");byte[] raw=new byte[size];Rules.Require(QueryServiceObjectSecurity(service,5,raw,size,out size),"hcs_service_acl_query_unknown");
    return new System.Collections.Generic.Dictionary<string,object>{{"service_name","vmcompute"},{"service_type",c.Type},{"service_state",s.State},{"service_pid",s.Pid},{"start_type",c.Start},{"service_account_is_system",account=="LocalSystem"},{"binary_exact_system32_vmcompute",HcsPure.ImageCommand(BinaryPath,System.IO.Path.Combine(system,"vmcompute.exe"))},{"binary_command_sha256",Rules.Hash(System.Text.Encoding.UTF8.GetBytes(BinaryPath??""))},{"service_sid_sha256",HcsPure.HashSid(HcsPure.ServiceSid("vmcompute"))},{"service_acl",Describe(new System.Security.AccessControl.RawSecurityDescriptor(raw,0),false)}};
   }finally{System.Runtime.InteropServices.Marshal.FreeHGlobal(p);}
  }
  public static System.Collections.Generic.Dictionary<string,object> Describe(System.Security.AccessControl.RawSecurityDescriptor sd,bool file){
   Rules.Require(sd.Owner!=null&&sd.DiscretionaryAcl!=null&&sd.DiscretionaryAcl.Count<=128,"hcs_acl_shape_unknown");var rows=new System.Collections.Generic.List<object>();bool unknownWriter=false;
   foreach(System.Security.AccessControl.GenericAce a in sd.DiscretionaryAcl){var k=a as System.Security.AccessControl.KnownAce;var q=a as System.Security.AccessControl.QualifiedAce;var common=a as System.Security.AccessControl.CommonAce;bool supported=common!=null&&!common.IsCallback;bool allow=q!=null&&q.AceQualifier==System.Security.AccessControl.AceQualifier.AccessAllowed;uint mask=k==null?0:unchecked((uint)k.AccessMask);string role=k==null?"unknown":file?HcsPure.FileRole(k.SecurityIdentifier.Value):MetadataRoles.Sid(k.SecurityIdentifier.Value,"","");
    // Service CHANGE_CONFIG/START/STOP/PAUSE/USER_CONTROL, generic execute,
    // DELETE and DAC/OWNER; file data/append,
    // delete-child/delete/DAC/OWNER. Unsupported ACEs remain unclassified.
    bool writer=HcsPure.ControlMutator(mask,allow,file);
    if(!supported||role=="unclassified"&&writer)unknownWriter=true;
    rows.Add(new System.Collections.Generic.Dictionary<string,object>{{"sid_role",role},{"sid_sha256",k==null?"":HcsPure.HashSid(k.SecurityIdentifier.Value)},{"mask",mask},{"flags",(int)a.AceFlags},{"common_noncallback",supported},{"qualifier",allow?"allow":q!=null&&q.AceQualifier==System.Security.AccessControl.AceQualifier.AccessDenied?"deny":"unknown"},{"mutating_access",writer}});
   }
   string owner=file?HcsPure.FileRole(sd.Owner.Value):MetadataRoles.Sid(sd.Owner.Value,"","");string sddl=sd.GetSddlForm(System.Security.AccessControl.AccessControlSections.Owner|System.Security.AccessControl.AccessControlSections.Access);
   return new System.Collections.Generic.Dictionary<string,object>{{"owner_role",owner},{"owner_sid_sha256",HcsPure.HashSid(sd.Owner.Value)},{"sddl_sha256",Rules.Hash(System.Text.Encoding.UTF8.GetBytes(sddl))},{"dacl_protected",(sd.ControlFlags&System.Security.AccessControl.ControlFlags.DiscretionaryAclProtected)!=0},{"unclassified_writer_present",unknownWriter},{"aces",rows},{"authority_granted",false}};
  }
  public void Dispose(){if(service!=System.IntPtr.Zero){CloseServiceHandle(service);service=System.IntPtr.Zero;}if(manager!=System.IntPtr.Zero){CloseServiceHandle(manager);manager=System.IntPtr.Zero;}}
 }
 public static class HcsAuthzModel {
  [System.Runtime.InteropServices.StructLayout(System.Runtime.InteropServices.LayoutKind.Sequential)]struct Luid{public uint Low;public int High;}
  [System.Runtime.InteropServices.StructLayout(System.Runtime.InteropServices.LayoutKind.Sequential)]struct SidAttr{public System.IntPtr Sid;public uint Attributes;}
  [System.Runtime.InteropServices.StructLayout(System.Runtime.InteropServices.LayoutKind.Sequential)]struct Groups{public uint Count;public SidAttr First;}
  [System.Runtime.InteropServices.StructLayout(System.Runtime.InteropServices.LayoutKind.Sequential)]struct Request{public uint Desired;public System.IntPtr Self,ObjectTypes;public uint Count;public System.IntPtr Optional;}
  [System.Runtime.InteropServices.StructLayout(System.Runtime.InteropServices.LayoutKind.Sequential)]struct Reply{public uint Count;public System.IntPtr Granted,Sacl,Error;}
  [System.Runtime.InteropServices.DllImport("authz.dll",CharSet=System.Runtime.InteropServices.CharSet.Unicode,SetLastError=true)]static extern bool AuthzInitializeResourceManager(uint flags,System.IntPtr access,System.IntPtr groups,System.IntPtr free,string name,out System.IntPtr manager);
  [System.Runtime.InteropServices.DllImport("authz.dll",SetLastError=true)]static extern bool AuthzInitializeContextFromSid(uint flags,System.IntPtr sid,System.IntPtr manager,System.IntPtr expiration,Luid identifier,System.IntPtr args,out System.IntPtr context);
  [System.Runtime.InteropServices.DllImport("authz.dll",SetLastError=true)]static extern bool AuthzAddSidsToContext(System.IntPtr context,SidAttr[] groups,uint count,System.IntPtr restricted,uint restrictedCount,out System.IntPtr copy);
  [System.Runtime.InteropServices.DllImport("authz.dll",SetLastError=true)]static extern bool AuthzSetAppContainerInformation(System.IntPtr context,System.IntPtr appSid,uint count,SidAttr[] caps);
  [System.Runtime.InteropServices.DllImport("authz.dll",SetLastError=true)]static extern bool AuthzGetInformationFromContext(System.IntPtr context,int kind,uint size,out uint needed,System.IntPtr buffer);
  [System.Runtime.InteropServices.DllImport("authz.dll",SetLastError=true)]static extern bool AuthzAccessCheck(uint flags,System.IntPtr context,ref Request request,System.IntPtr audit,byte[] sd,System.IntPtr optional,uint optionalCount,ref Reply reply,System.IntPtr cache);
  [System.Runtime.InteropServices.DllImport("authz.dll")]static extern bool AuthzFreeContext(System.IntPtr p);
  [System.Runtime.InteropServices.DllImport("authz.dll")]static extern bool AuthzFreeResourceManager(System.IntPtr p);
  [System.Runtime.InteropServices.DllImport("userenv.dll",CharSet=System.Runtime.InteropServices.CharSet.Unicode)]static extern int DeriveAppContainerSidFromAppContainerName(string name,out System.IntPtr sid);
  [System.Runtime.InteropServices.DllImport("advapi32.dll")]static extern System.IntPtr FreeSid(System.IntPtr p);
  [System.Runtime.InteropServices.DllImport("kernelbase.dll",CharSet=System.Runtime.InteropServices.CharSet.Unicode,SetLastError=true)]static extern bool DeriveCapabilitySidsFromName(string name,out System.IntPtr groups,out uint groupCount,out System.IntPtr caps,out uint capCount);
  [System.Runtime.InteropServices.DllImport("kernel32.dll")]static extern System.IntPtr LocalFree(System.IntPtr p);
  static void FreeArray(System.IntPtr array,uint count){if(array==System.IntPtr.Zero)return;if(count<=16)for(int i=0;i<count;i++)LocalFree(System.Runtime.InteropServices.Marshal.ReadIntPtr(array,i*System.IntPtr.Size));LocalFree(array);}
  static System.IntPtr Sid(string text){var sid=new System.Security.Principal.SecurityIdentifier(text);byte[] b=new byte[sid.BinaryLength];sid.GetBinaryForm(b,0);var p=System.Runtime.InteropServices.Marshal.AllocHGlobal(b.Length);System.Runtime.InteropServices.Marshal.Copy(b,0,p,b.Length);return p;}
  static System.Collections.Generic.List<string> Info(System.IntPtr ctx,int kind){uint n;AuthzGetInformationFromContext(ctx,kind,0,out n,System.IntPtr.Zero);Rules.Require(n>=4&&n<=65536,"hcs_context_info_size_unknown");var p=System.Runtime.InteropServices.Marshal.AllocHGlobal((int)n);try{Rules.Require(AuthzGetInformationFromContext(ctx,kind,n,out n,p),"hcs_context_info_unknown");int count=System.Runtime.InteropServices.Marshal.ReadInt32(p);int offset=(int)System.Runtime.InteropServices.Marshal.OffsetOf(typeof(Groups),"First"),stride=System.Runtime.InteropServices.Marshal.SizeOf(typeof(SidAttr));Rules.Require(count>=0&&count<=128&&(count==0||offset+count*stride<=n),"hcs_context_info_shape_unknown");var rows=new System.Collections.Generic.List<string>();for(int i=0;i<count;i++){var item=(SidAttr)System.Runtime.InteropServices.Marshal.PtrToStructure(System.IntPtr.Add(p,offset+i*stride),typeof(SidAttr));rows.Add(new System.Security.Principal.SecurityIdentifier(item.Sid).Value);}return rows;}finally{System.Runtime.InteropServices.Marshal.FreeHGlobal(p);}}
  static object Check(System.IntPtr ctx,byte[] descriptor,uint desired){var granted=System.Runtime.InteropServices.Marshal.AllocHGlobal(4);var error=System.Runtime.InteropServices.Marshal.AllocHGlobal(4);try{System.Runtime.InteropServices.Marshal.WriteInt32(granted,0);System.Runtime.InteropServices.Marshal.WriteInt32(error,0);var req=new Request{Desired=desired};var rep=new Reply{Count=1,Granted=granted,Error=error};bool ok=AuthzAccessCheck(0,ctx,ref req,System.IntPtr.Zero,descriptor,System.IntPtr.Zero,0,ref rep,System.IntPtr.Zero);int apiError=ok?0:System.Runtime.InteropServices.Marshal.GetLastWin32Error();uint g=unchecked((uint)System.Runtime.InteropServices.Marshal.ReadInt32(granted)),e=unchecked((uint)System.Runtime.InteropServices.Marshal.ReadInt32(error));return new System.Collections.Generic.Dictionary<string,object>{{"desired",desired},{"api_succeeded",ok},{"api_error",apiError},{"granted",g},{"access_error",e},{"allowed",ok&&e==0&&(g&desired)==desired},{"status",!ok?"UNKNOWN":e==0&&(g&desired)==desired?"ALLOW":e==5?"DENY":"UNKNOWN"}};}finally{System.Runtime.InteropServices.Marshal.FreeHGlobal(granted);System.Runtime.InteropServices.Marshal.FreeHGlobal(error);}}
  static object Context(System.IntPtr manager,string user,bool hvg,bool app,bool capability,string cap,System.IntPtr appSid,byte[] descriptor){
   System.IntPtr baseCtx=System.IntPtr.Zero,ctx=System.IntPtr.Zero,userPtr=System.IntPtr.Zero,everyone=System.IntPtr.Zero,group=System.IntPtr.Zero,capPtr=System.IntPtr.Zero;
   try{userPtr=Sid(user);everyone=Sid("S-1-1-0");Rules.Require(AuthzInitializeContextFromSid(2,userPtr,manager,System.IntPtr.Zero,new Luid(),System.IntPtr.Zero,out baseCtx),"hcs_context_initialize_unknown");var groups=new System.Collections.Generic.List<SidAttr>();groups.Add(new SidAttr{Sid=everyone,Attributes=4});if(hvg){group=Sid("S-1-5-32-578");groups.Add(new SidAttr{Sid=group,Attributes=4});}Rules.Require(AuthzAddSidsToContext(baseCtx,groups.ToArray(),(uint)groups.Count,System.IntPtr.Zero,0,out ctx),"hcs_context_groups_unknown");
    if(app){capPtr=Sid(cap);Rules.Require(AuthzSetAppContainerInformation(ctx,appSid,capability?1U:0U,capability?new SidAttr[]{new SidAttr{Sid=capPtr,Attributes=4}}:null),"hcs_context_app_capability_unknown");}
    var actualGroups=Info(ctx,2);var actualCaps=Info(ctx,16);bool separated=HcsPure.CapabilitySeparation(actualGroups.ToArray(),actualCaps.ToArray(),hvg,capability,cap);Rules.Require(separated,"hcs_context_capability_separation_unknown");
    var result=new System.Collections.Generic.List<object>();bool checksObserved=true;foreach(uint mask in new uint[]{2,4,0x40000,0x80000}){var row=(System.Collections.Generic.Dictionary<string,object>)Check(ctx,descriptor,mask);if((string)row["status"]=="UNKNOWN")checksObserved=false;result.Add(row);}
    return new System.Collections.Generic.Dictionary<string,object>{{"model_status",checksObserved?"OBSERVED":"PARTIAL_OR_UNKNOWN"},{"app_container_model",app},{"capability_requested",capability},{"normal_group_contains_capability",actualGroups.Contains(cap)},{"capability_list_contains_target",actualCaps.Contains(cap)},{"normal_group_count",actualGroups.Count},{"capability_count",actualCaps.Count},{"normal_and_capability_separated",separated},{"checks",result},{"real_token_tested",false}};
   }catch{return new System.Collections.Generic.Dictionary<string,object>{{"model_status","UNKNOWN"},{"api_error",System.Runtime.InteropServices.Marshal.GetLastWin32Error()},{"app_container_model",app},{"capability_requested",capability},{"real_token_tested",false}};}
   finally{if(ctx!=System.IntPtr.Zero)AuthzFreeContext(ctx);if(baseCtx!=System.IntPtr.Zero)AuthzFreeContext(baseCtx);foreach(var p in new System.IntPtr[]{userPtr,everyone,group,capPtr})if(p!=System.IntPtr.Zero)System.Runtime.InteropServices.Marshal.FreeHGlobal(p);}
  }
  public static System.Collections.Generic.Dictionary<string,object> Run(string vm,string expectedPipeHash){var output=new System.Collections.Generic.Dictionary<string,object>();System.IntPtr groups=System.IntPtr.Zero,caps=System.IntPtr.Zero,app=System.IntPtr.Zero,manager=System.IntPtr.Zero;uint ng=0,nc=0;
   try{Rules.Require(DeriveCapabilitySidsFromName("vmWorkerProcess",out groups,out ng,out caps,out nc)&&ng<=16&&nc==1,"hcs_capability_derive_unknown");string cap=new System.Security.Principal.SecurityIdentifier(System.Runtime.InteropServices.Marshal.ReadIntPtr(caps)).Value;bool capMatch=HcsPure.HashSid(cap)=="59217592fcdabf0b9b76ab2987b0dce343b526fb9639c8e749d269f4e99797ce";output["capability_sid_sha256"]=HcsPure.HashSid(cap);output["capability_matches_observed"]=capMatch;
    bool vmMatch=Rules.VirtualMachineSid(vm);output["expected_vm_sid_sha256"]=HcsPure.HashSid(vm);output["expected_vm_matches_observed"]=vmMatch;
    var acl=new System.Security.AccessControl.RawAcl(2,6);string[] ids={"S-1-1-0","S-1-5-18","S-1-5-32-544","S-1-5-32-578",vm,cap};for(int i=0;i<ids.Length;i++)acl.InsertAce(i,new System.Security.AccessControl.CommonAce(0,System.Security.AccessControl.AceQualifier.AccessAllowed,i==0?1179785:2032127,new System.Security.Principal.SecurityIdentifier(ids[i]),false,null));var sd=new System.Security.AccessControl.RawSecurityDescriptor(System.Security.AccessControl.ControlFlags.DiscretionaryAclPresent|System.Security.AccessControl.ControlFlags.SelfRelative,new System.Security.Principal.SecurityIdentifier("S-1-5-18"),null,null,acl);string hash=Rules.Hash(System.Text.Encoding.UTF8.GetBytes(sd.GetSddlForm(System.Security.AccessControl.AccessControlSections.Owner|System.Security.AccessControl.AccessControlSections.Access)));output["reconstructed_pipe_sddl_sha256"]=hash;bool same=hash==expectedPipeHash;output["six_ace_descriptor_matches_observed"]=same;Rules.Require(capMatch&&vmMatch&&same,"hcs_model_descriptor_pin_unknown");byte[] descriptor=new byte[sd.BinaryLength];sd.GetBinaryForm(descriptor,0);
    Rules.Require(AuthzInitializeResourceManager(1,System.IntPtr.Zero,System.IntPtr.Zero,System.IntPtr.Zero,"HaHcsReadOnlyMemoryModel",out manager),"hcs_authz_manager_unknown");int appHr=DeriveAppContainerSidFromAppContainerName("HaHcsReadOnlyMemoryModel",out app);output["app_sid_derive_hresult"]=unchecked((uint)appHr);output["app_profile_registered"]=false;output["new_token_created"]=false;
    var cases=new System.Collections.Generic.Dictionary<string,object>();string unpriv="S-1-5-21-100-200-300-1001",foreign="S-1-5-83-1-101-202-303-404-505";
    cases["normal_unpriv_everyone"]=Context(manager,unpriv,false,false,false,cap,app,descriptor);cases["normal_foreign_vm"]=Context(manager,foreign,false,false,false,cap,app,descriptor);cases["normal_own_vm"]=Context(manager,vm,false,false,false,cap,app,descriptor);cases["normal_hvg"]=Context(manager,unpriv,true,false,false,cap,app,descriptor);
    if(appHr==0&&app!=System.IntPtr.Zero){cases["app_unpriv_with_cap"]=Context(manager,unpriv,false,true,true,cap,app,descriptor);cases["app_foreign_vm_with_cap"]=Context(manager,foreign,false,true,true,cap,app,descriptor);cases["app_own_vm_with_cap"]=Context(manager,vm,false,true,true,cap,app,descriptor);cases["app_hvg_without_cap"]=Context(manager,unpriv,true,true,false,cap,app,descriptor);}output["cases"]=cases;bool all=true;foreach(object row in cases.Values){var record=(System.Collections.Generic.Dictionary<string,object>)row;if((string)record["model_status"]!="OBSERVED")all=false;}output["all_contexts_observed"]=all;output["model_status"]=all&&cases.Count==8?"OBSERVED":"PARTIAL_OR_UNKNOWN";
   }catch{output["model_status"]="UNKNOWN";output["api_error"]=System.Runtime.InteropServices.Marshal.GetLastWin32Error();}finally{if(manager!=System.IntPtr.Zero)AuthzFreeResourceManager(manager);if(app!=System.IntPtr.Zero)FreeSid(app);FreeArray(groups,ng);FreeArray(caps,nc);}output["real_token_tested"]=false;output["actual_thread_or_future_token_acquisition_proved_impossible"]=false;output["authority_granted"]=false;return output;
  }
 }
#endif

#if !PORTABLE_FIXTURE
 public sealed class RegisteredHcsLease:IDisposable {
  readonly Dictionary<string,object> profile;readonly string system;HcsServiceLease service;MetadataProcessLease process;MetadataImageLease image;
  public readonly uint Pid;public readonly long StartTicks;public readonly string PipeDescriptorSha256;
  public string ImagePath{get{return System.IO.Path.Combine(system,"vmcompute.exe");}}
  public RegisteredHcsLease(Dictionary<string,object> p,string vmSid){profile=p;system=Environment.GetFolderPath(Environment.SpecialFolder.System);Pid=(uint)Rules.Integer(p,"expected_pid");StartTicks=Int64.Parse(Rules.Text(p,"expected_start_ticks"),System.Globalization.CultureInfo.InvariantCulture);PipeDescriptorSha256=Rules.Text(p,"expected_pipe_sddl_sha256");
   try{service=new HcsServiceLease();Rules.Require(HostProfile.ServiceBinding(service.Query(system),profile),"guest_com_host_service_rejected");process=new MetadataProcessLease(Pid);image=new MetadataImageLease(ImagePath,system);Rules.Require(image.FileIdentity==Rules.Text(p,"expected_file_identity")&&image.Sha256==Rules.Text(p,"expected_image_sha256"),"guest_com_host_image_pin_rejected");var model=HcsAuthzModel.Run(vmSid,PipeDescriptorSha256);Rules.Require(HostProfile.ModelAccepted(model,PipeDescriptorSha256),"guest_com_host_memory_model_rejected");AssertCurrent();}catch{Dispose();throw;}
  }
  public void AssertCurrent(){
   Rules.Require(HostProfile.ServiceBinding(service.Query(system),profile)&&process.Stable(),"guest_com_host_service_changed");var f=process.Facts("","",system,0,0);
   Rules.Require(HostProfile.ProcessBinding(f,profile,process.ImagePath,ImagePath),"guest_com_host_process_changed");
   Rules.Require(image.IdentityStable(),"guest_com_host_image_changed");var acl=HcsServiceLease.Describe(image.Descriptor(),true);
   Rules.Require(HostProfile.SafeControlAcl(acl,true)&&(string)acl["sddl_sha256"]==Rules.Text(profile,"expected_file_sddl_sha256"),"guest_com_host_file_acl_changed");
  }
  public void FinalizeFile(){AssertCurrent();Rules.Require(image.Stable(),"guest_com_host_image_changed");}
  public void Dispose(){if(image!=null){image.Dispose();image=null;}if(process!=null){process.Dispose();process=null;}if(service!=null){service.Dispose();service=null;}}
 }

#endif

}
'@
function Import-GuestOwnedSupport{
 $path=Join-Path $PSScriptRoot 'guest-owned-profile.ps1';$bytes=[IO.File]::ReadAllBytes($path)
 if((Get-GuestComHash $bytes) -cne '437d02933c2800cea4097a79ee14db3052e8c522c15f132a52c7c094d6f84045'){throw 'guest_owned_support_changed'}
 return [scriptblock]::Create([Text.UTF8Encoding]::new($false,$true).GetString($bytes))
}
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
function Get-GuestComHostProfilePaths([string]$Root,$Spec){
    # Version 3 keeps each node's immutable evidence separate within one installation.
    $version=[HaGuestComV1.Rules]::Integer($Spec,'schema_version')
    $node=[HaGuestComV1.Rules]::Text($Spec,'node')
    if($version -notin @(2,3,4) -or $node -cnotin @('A','B')){throw 'guest_com_host_profile_rejected'}
    if($version -eq 4 -and (-not $Spec.ContainsKey('root_profile') -or $Spec['root_profile'] -cne 'strict-canonical-observed-v1')){throw 'guest_com_owned_profile_required'}
    $prefix=if($version -ge 3){'com-'+$node+'-host-compute'}else{'com-host-compute'}
    return [pscustomobject]@{profile=(Join-Path $Root ($prefix+'-profile.json'));evidence=(Join-Path $Root ($prefix+'-evidence.json'))}
}
function Read-GuestComRequest{
    if($RequestSha256 -cnotmatch '^[0-9a-f]{64}$' -or $Request.Length -gt 32768){throw 'guest_com_request_pin_required'}
    $raw=[Convert]::FromBase64String($Request)
    if((Get-GuestComHash $raw) -cne $RequestSha256){throw 'guest_com_request_hash_changed'}
    $value=[HaGuestComV1.Json]::Object(([Text.UTF8Encoding]::new($false,$true)).GetString($raw),16384)
    $keys=@('schema_version','collector_source_commit','vm_source_commit','stage_source_commit','seed_source_commit','installation_id','node','vm_id','prepared_receipt_sha256','seed_id','bootstrap_manifest_sha256','expected_kernel_release','expected_pg_image','expected_package_count','timeout_seconds','egress_mac_address','peer_prepared_receipt_sha256','switch_owner_manifest_sha256')
    $version=[HaGuestComV1.Rules]::Integer($value,'schema_version')
    if($version -in @(2,3,4)){$keys+=@('pipe_profile','host_profile_sha256','host_evidence_sha256')}
    elseif($version -ne 1){throw 'guest_com_request_version_rejected'}
    if($version -eq 4){. (Import-GuestOwnedSupport);$keys+=@(Get-GuestOwnedFields);Assert-GuestOwnedDeclaration ([pscustomobject]$value) '';if($value['executor_source_commit'] -cne $value['collector_source_commit']){throw 'guest_com_executor_source_changed'}}
    [HaGuestComV1.Rules]::Keys($value,$keys)
    if($version -in @(2,3,4)){
        if([HaGuestComV1.Rules]::Text($value,'pipe_profile') -cne 'registered-host-compute-v1'){throw 'guest_com_host_profile_rejected'}
        foreach($key in @('host_profile_sha256','host_evidence_sha256')){if(-not [HaGuestComV1.Rules]::Match([HaGuestComV1.Rules]::Text($value,$key),'[0-9a-f]{64}')){throw 'guest_com_host_profile_pin_rejected'}}
    }
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
        if($null -ne $script:guestOwnedSpec -and $current -ieq $stop -and $stop -ieq $script:guestOwnedRoot){Assert-GuestOwnedRootAcl $current $script:guestOwnedSpec;break}
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
    $allPorts=@(Get-VMComPort -VM $vm);$ports=@($allPorts|Where-Object{$_.Name -ceq 'COM 1'});$otherPorts=@($allPorts|Where-Object{$_.Name -ceq 'COM 2'})
    if($allPorts.Count -ne 2 -or $ports.Count -ne 1 -or $otherPorts.Count -ne 1 -or
        @($allPorts|Where-Object{[string]$_.VMId -ine $Spec['vm_id']}).Count -ne 0 -or
        $ports[0].Path -cne $Pins['com1_pipe'] -or $otherPorts[0].Path -notin @($null,'')){throw 'guest_com_current_com_path_changed'}
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
function Assert-GuestComHostSignature($Lease,$Profile){
    $sig=Get-AuthenticodeSignature -LiteralPath $Lease.ImagePath -ErrorAction Stop
    if([string]$sig.Status -cne 'Valid' -or $null -eq $sig.SignerCertificate -or
        $sig.SignerCertificate.GetNameInfo([Security.Cryptography.X509Certificates.X509NameType]::SimpleName,$false) -cnotin @('Microsoft Windows','Microsoft Corporation','Microsoft Windows Publisher','Microsoft Windows Hardware Compatibility Publisher') -or
        $sig.SignerCertificate.Subject -notmatch '(?:^|,\s*)O=Microsoft Corporation(?:,|$)' -or
        (Get-GuestComHash $sig.SignerCertificate.RawData) -cne $Profile['expected_signer_certificate_sha256']){throw 'guest_com_host_signer_rejected'}
}
function Assert-GuestComReaderBoundary($Reader,$Process,$HostLease,[string]$Caller,[string]$VmSid){
    $Process.AssertAlive()
    if($null -eq $HostLease){$Reader.Verify($Process.Pid,$Caller,$VmSid)}
    else{$HostLease.AssertCurrent();$Reader.VerifyRegistered($HostLease.Pid,$HostLease.PipeDescriptorSha256);$HostLease.AssertCurrent()}
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
    $ownedLease=$null
    if($spec['schema_version'] -eq 4){. (Import-GuestOwnedSupport);$script:guestOwnedSpec=[pscustomobject]$spec;$script:guestOwnedRoot=$root;$ownedLease=Open-GuestOwnedRoot $root $script:guestOwnedSpec (Get-GuestComHash ([IO.File]::ReadAllBytes($PSCommandPath)))}
    try{
    $path=Join-Path $root ('vm-'+$spec['node']+'-prepared-receipt.json')
    Assert-GuestComAncestors $path $root $caller
    $file=$null;$process=$null;$reader=$null;$profileFile=$null;$evidenceFile=$null;$hostLease=$null;$hostProfile=$null
    try{
        $file=[HaGuestComV1.PrivateFile]::new($path,$spec['prepared_receipt_sha256'],$caller)
        $pins=Read-GuestComPrepared $file $spec $root
        $initial=Get-GuestComVm $spec $pins
        $vmSid=([Security.Principal.NTAccount]::new('NT VIRTUAL MACHINE\'+$spec['vm_id'])).Translate([Security.Principal.SecurityIdentifier]).Value
        if(-not [HaGuestComV1.Rules]::VirtualMachineSid($vmSid)){throw 'guest_com_vm_sid_rejected'}
        if($spec['schema_version'] -in @(2,3,4)){
            # These independent external pins bind root-admin-reviewed original
            # evidence bytes and an explicit private policy. Neither substitutes
            # for current live identity, descriptor and memory-model validation.
            $paths=Get-GuestComHostProfilePaths $root $spec;$profilePath=$paths.profile;$evidencePath=$paths.evidence
            Assert-GuestComAncestors $profilePath $root $caller;Assert-GuestComAncestors $evidencePath $root $caller
            $profileFile=[HaGuestComV1.PrivateFile]::new($profilePath,$spec['host_profile_sha256'],$caller)
            $evidenceFile=[HaGuestComV1.PrivateFile]::new($evidencePath,$spec['host_evidence_sha256'],$caller)
            $hostProfile=[HaGuestComV1.HostProfile]::Parse(([Text.UTF8Encoding]::new($false,$true)).GetString($profileFile.Bytes),$spec)
            $hostLease=[HaGuestComV1.RegisteredHcsLease]::new($hostProfile,$vmSid)
            Assert-GuestComHostSignature $hostLease $hostProfile
        }
        if($initial.State -cne 'Off' -or $initial.Pid -ne 0){throw 'guest_com_reader_requires_fresh_off_vm'}
        $beforeReady=Get-GuestComVm $spec $pins
        if($beforeReady.State -cne 'Off' -or $beforeReady.Pid -ne 0){throw 'guest_com_reader_requires_fresh_off_vm'}
        $readyHash=New-GuestComReaderReady $spec $pins $root $caller
        $frame=[HaGuestComV1.Framer]::new();$clock=[Diagnostics.Stopwatch]::StartNew();$connections=0
        while($clock.Elapsed.TotalSeconds -lt $spec['timeout_seconds']){
            $file.AssertUnchanged();if($null -ne $profileFile){$profileFile.AssertUnchanged();$evidenceFile.AssertUnchanged()};$current=Get-GuestComVm $spec $pins
            if(-not $current.Running){if($null -ne $process){throw 'guest_com_vm_stopped_after_connection'};Start-Sleep -Milliseconds 200;continue}
            if($current.Pid -eq 0){if($null -ne $process){throw 'guest_com_vmwp_unknown'};Start-Sleep -Milliseconds 200;continue}
            if($null -eq $process){$process=[HaGuestComV1.ProcessLease]::new($current.Pid,(Join-Path ([Environment]::GetFolderPath([Environment+SpecialFolder]::System)) 'vmwp.exe'),$vmSid)}
            if($process.Pid -ne $current.Pid){throw 'guest_com_vmwp_replaced'}
            $process.AssertAlive()
            if($null -eq $reader){
                if($connections -ge 2){throw 'guest_com_reconnect_limit'}
                $reader=[HaGuestComV1.Reader]::TryConnect($pins['com1_pipe'].Substring(9),500)
                if($null -eq $reader){continue}
                $connections++;$frame.ResetConnection();Assert-GuestComReaderBoundary $reader $process $hostLease $caller $vmSid
            }
            $chunk=$null
            try{$chunk=$reader.Read(500)}catch [IO.IOException]{$reader.Dispose();$reader=$null;continue}
            if($null -eq $chunk){continue}
            if($chunk.Length -eq 0){$reader.Dispose();$reader=$null;continue}
            $payload=$frame.Feed($chunk,$chunk.Length)
            if($null -eq $payload){continue}
            $guest=[HaGuestComV1.Rules]::Receipt($payload,$spec)
            Assert-GuestComReaderBoundary $reader $process $hostLease $caller $vmSid;$process.AssertAlive();$after=Get-GuestComVm $spec $pins
            if(-not $after.Running -or $after.Pid -ne $process.Pid){throw 'guest_com_vmwp_changed_after_receipt'}
            $file.AssertUnchanged();Assert-GuestComAncestors $path $root $caller
            if($clock.Elapsed.TotalSeconds -ge $spec['timeout_seconds']){throw 'guest_com_timeout_unknown'}
            if($guest['status'] -cne 'PASS'){throw 'guest_com_guest_reported_failure'}
            if($null -ne $hostLease){$profileFile.AssertUnchanged();$evidenceFile.AssertUnchanged();Assert-GuestComHostSignature $hostLease $hostProfile;$hostLease.FinalizeFile();Assert-GuestComReaderBoundary $reader $process $hostLease $caller $vmSid}
            $finalVm=Get-GuestComVm $spec $pins;if(-not $finalVm.Running -or $finalVm.Pid -ne $process.Pid){throw 'guest_com_vmwp_changed_after_receipt'}
            if($clock.Elapsed.TotalSeconds -ge $spec['timeout_seconds']){throw 'guest_com_timeout_unknown'}
            $hostBoundary=if($null -eq $hostLease){$null}else{[ordered]@{trust_model='HostHypervisorTCB';scope='isolated_predeployment_guest_enrollment';profile_sha256=$spec['host_profile_sha256'];external_evidence_sha256=$spec['host_evidence_sha256'];external_evidence_pin_verified=$true;server_pid=$hostLease.Pid;server_start_ticks=$hostLease.StartTicks;loaded_image_file_authenticated=$false;kernel_creator_authenticated=$false;production_actor_closure_verified=$false;writer_authority_issued=$false}}
            $answer=[ordered]@{status='PASS';scope='owned_guest_retained_com_public_key_receipt';collector_source_commit=$spec['collector_source_commit'];reader_ready_sha256=$readyHash;vm_source_commit=$spec['vm_source_commit'];stage_source_commit=$spec['stage_source_commit'];seed_source_commit=$spec['seed_source_commit'];installation_id=$spec['installation_id'];node=$spec['node'];vm_id=$spec['vm_id'];prepared_receipt_sha256=$spec['prepared_receipt_sha256'];vmwp_pid=$process.Pid;vmwp_start_ticks=$process.StartTicks;connections=$connections;bytes_read=$frame.Bytes;lines_seen=$frame.Lines;native_host_boundary_verified=$true;pipe_server_pid_verified=$true;pipe_acl_verified=$true;current_vm_com_path_verified=$true;guest_receipt=$guest;windows_fence_verified=$false;writer_authority_issued=$false;production_transition=$false}
            if($null -ne $hostLease){$answer['schema_version']=2;$answer['scope']='owned_guest_registered_host_compute_public_key_receipt';$answer['pipe_boundary_profile']='registered-host-compute-v1';$answer['registered_host_compute']=$hostBoundary}
            return $answer
        }
        throw 'guest_com_timeout_unknown'
    }finally{$disposeFailed=$false;foreach($resource in @($reader,$process,$hostLease,$evidenceFile,$profileFile,$file)){if($null -ne $resource){try{$resource.Dispose()}catch{$disposeFailed=$true}}};if($disposeFailed){throw 'guest_com_handle_cleanup_unknown'}}
    }finally{if($null -ne $ownedLease){$ownedLease.Dispose()}}
}
if($MyInvocation.InvocationName -ne '.'){
    try{Invoke-GuestComReceipt|ConvertTo-Json -Depth 8 -Compress}catch{
        $reason='guest_com_unknown';$message=$_.Exception.GetBaseException().Message
        if($message -cmatch '^guest_com_[a-z0-9_]{1,100}$'){$reason=$message}
        [ordered]@{status='UNKNOWN';reason=$reason;native_host_boundary_verified=$false;production_transition=$false}|ConvertTo-Json -Compress
        exit 1
    }
}
