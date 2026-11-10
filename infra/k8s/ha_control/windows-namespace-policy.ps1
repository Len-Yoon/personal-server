param(
 [ValidateSet('inspect','plan','apply','rollback')][string]$Mode='inspect',
 [string]$InstallationId,[string]$SourceCommit,[string]$ScriptSha256,
 [string]$ManifestPath,[string]$ManifestSha256,[string]$PlanSha256,[string]$ApplyReceiptSha256,
 [switch]$Win64DesktopAdmin,[switch]$Go
)
# Namespace-wide trust, never per-VM authorization. No SCM/group/service action.
# Inspect/plan remain read-only even with -Go; SD readback alone does not prove
# a VM power transition, writer ownership, or production HA acceptance.
# MS WMI inheritance differs from NTFS. A protected/materialized candidate is
# not proof that the provider will accept it: fresh external provider evidence
# and exact native readback are required. No automatic rollback or retry.
# Future provider evidence: separately reviewed new UUID test namespace only,
# copy this SD shape, exercise parent/child inheritance and exact SetSD/GetSD
# roundtrip + original rollback + owned fixture deletion. Not implemented here;
# never use root/virtualization/v2 as an unreviewed semantics experiment.
# This rollback accepts a completed exact apply receipt/target only. An uncertain
# partial apply remains preserved for separate reviewed manual reconciliation.
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
function ConvertTo-NpMode([string]$Value){
 # ValidateSet accepts mixed case. Dispatch must use one canonical value so an
 # Inspect/Plan spelling can never fall through into a transaction branch.
 if([string]::IsNullOrEmpty($Value)){throw 'namespace_mode_rejected'}
 $canonical=$Value.ToLowerInvariant()
 if($canonical -cnotin @('inspect','plan','apply','rollback')){throw 'namespace_mode_rejected'}
 return $canonical
}
function Get-NpHash([byte[]]$Bytes){$h=[Security.Cryptography.SHA256]::Create();try{return [BitConverter]::ToString($h.ComputeHash($Bytes)).Replace('-','').ToLowerInvariant()}finally{$h.Dispose()}}
function Get-NpJsonHash($Value){return Get-NpHash ([Text.Encoding]::UTF8.GetBytes((ConvertTo-Json -InputObject $Value -Depth 12 -Compress)))}
function Assert-NpKeys($Value,[string[]]$Keys){if($null -eq $Value -or (($Value.PSObject.Properties.Name|Sort-Object -CaseSensitive)-join ',') -cne (($Keys|Sort-Object -CaseSensitive)-join ',')){throw 'namespace_shape_rejected'}}
function Read-NpJson([byte[]]$Bytes){
 if($Bytes.Length -lt 2 -or $Bytes.Length -gt 65536){throw 'namespace_json_limit'}
 $text=[Text.UTF8Encoding]::new($false,$true).GetString($Bytes)
 # Contracts contain no paths or escaped strings. Reject escapes BEFORE parsing
 # to prevent Unicode-escaped duplicate-key overwrites in Windows PowerShell.
 if($text.Contains('\')){throw 'namespace_json_escape_rejected'}
 $keys=@([regex]::Matches($text,'"([A-Za-z_][A-Za-z0-9_]*)"\s*:')|ForEach-Object{$_.Groups[1].Value})
 if($keys.Count -ne @($keys|Sort-Object -Unique).Count){throw 'namespace_json_duplicate_rejected'}
 return $text|ConvertFrom-Json
}
function Initialize-NpCore{
 if('HaNamespacePolicyCoreV1' -as [type]){throw 'namespace_type_collision'}
 Add-Type -Language CSharp -TypeDefinition @'
using System;using System.IO;using System.Text;using System.Collections.Generic;using System.Security.Cryptography;
public static class HaNamespacePolicyCoreV1 {
 static void Need(bool b){if(!b)throw new InvalidOperationException("namespace_descriptor_rejected");}
 static ushort U16(byte[] b,int o){return (ushort)(b[o]|b[o+1]<<8);}
 static uint U32(byte[] b,int o){return (uint)(b[o]|b[o+1]<<8|b[o+2]<<16|b[o+3]<<24);}
 static void W16(byte[] b,int o,int x){b[o]=(byte)x;b[o+1]=(byte)(x>>8);}
 static void W32(byte[] b,int o,uint x){for(int i=0;i<4;i++)b[o+i]=(byte)(x>>(i*8));}
 static byte[] Slice(byte[] b,int o,int n){Need(o>=0&&n>=0&&o<=b.Length-n);byte[] r=new byte[n];Buffer.BlockCopy(b,o,r,0,n);return r;}
 static int SidSize(byte[] b,int o){Need(o>=0&&o<=b.Length-8&&b[o]==1&&b[o+1]<=15);int n=8+4*b[o+1];Need(o<=b.Length-n);return n;}
 static string Sid(byte[] b,int o){int n=SidSize(b,o);ulong authority=0;for(int i=2;i<8;i++)authority=(authority<<8)|b[o+i];var s=new StringBuilder("S-1-"+authority);for(int i=8;i<n;i+=4)s.Append("-"+U32(b,o+i));return s.ToString();}
 static byte[] SidBytes(string text){string[] p=text.Split('-');Need(p.Length>=3&&p[0]=="S"&&p[1]=="1"&&p.Length<=18);ulong a;Need(UInt64.TryParse(p[2],out a)&&a<=0xffffffffffffUL);byte[] b=new byte[8+4*(p.Length-3)];b[0]=1;b[1]=(byte)(p.Length-3);for(int i=7;i>=2;i--){b[i]=(byte)a;a>>=8;}for(int i=3;i<p.Length;i++){uint x;Need(UInt32.TryParse(p[i],out x));W32(b,8+4*(i-3),x);}Need(Sid(b,0)==text);return b;}
 static byte[] Component(byte[] sd,int field,bool acl){uint value=U32(sd,field);if(value==0)return null;Need(value>=20&&value<=65536&&(value&3)==0);int o=(int)value;Need(o<sd.Length);int n;if(acl){Need(o<=sd.Length-8&&(sd[o]==2||sd[o]==4));n=U16(sd,o+2);Need(n>=8&&o<=sd.Length-n);}else n=SidSize(sd,o);return Slice(sd,o,n);}
 static List<byte[]> Aces(byte[] acl,bool dacl){var list=new List<byte[]>();if(acl==null)return list;int count=U16(acl,4),o=8;Need(count<=128);for(int i=0;i<count;i++){Need(o<=acl.Length-4);int n=U16(acl,o+2);Need(n>=4&&(n&3)==0&&o<=acl.Length-n);if(dacl){Need((acl[o]==0||acl[o]==1)&&n>=16&&n==8+SidSize(acl,o+8));}list.Add(Slice(acl,o,n));o+=n;}Need(o==acl.Length);return list;}
 static bool Same(byte[] a,byte[] b){if(a==null||b==null)return a==b;if(a.Length!=b.Length)return false;for(int i=0;i<a.Length;i++)if(a[i]!=b[i])return false;return true;}
 static void Validate(byte[] b){Need(b!=null&&b.Length>=20&&b.Length<=65536&&b[0]==1&&(U16(b,2)&0x8004)==0x8004);byte[] own=Component(b,4,false),group=Component(b,8,false),sacl=Component(b,12,true),dacl=Component(b,16,true);Need(own!=null&&group!=null&&dacl!=null&&Aces(dacl,true).Count>0);Aces(sacl,false);if((U16(b,2)&16)==0)Need(sacl==null);var spans=new List<int[]>();foreach(int field in new[]{4,8,12,16}){byte[] c=Component(b,field,field>=12);if(c!=null){int offset=(int)U32(b,field);foreach(int[] prior in spans)Need((offset==prior[0]&&offset+c.Length==prior[1])||offset>=prior[1]||offset+c.Length<=prior[0]);spans.Add(new[]{offset,offset+c.Length});}}}
 static byte[] Pack(byte[] original,byte[] dacl,ushort control){var parts=new[]{Component(original,4,false),Component(original,8,false),Component(original,12,true),dacl};using(var m=new MemoryStream()){m.Write(new byte[20],0,20);int[] offsets=new int[4];for(int i=0;i<parts.Length;i++){if(parts[i]==null)continue;while((m.Length&3)!=0)m.WriteByte(0);offsets[i]=(int)m.Length;m.Write(parts[i],0,parts[i].Length);}byte[] b=m.ToArray();b[0]=original[0];b[1]=original[1];W16(b,2,control);for(int i=0;i<4;i++)W32(b,4+i*4,(uint)offsets[i]);Validate(b);return b;}}
 public static string ServiceName(string installation){Need(installation!=null&&System.Text.RegularExpressions.Regex.IsMatch(installation,"\\A[0-9a-f]{64}\\z"));return "N100HA_"+installation.Substring(0,24)+"_controller";}
 public static string ServiceSid(string name){Need(name!=null&&System.Text.RegularExpressions.Regex.IsMatch(name,"\\AN100HA_[0-9a-f]{24}_controller\\z"));using(var h=SHA1.Create()){byte[] b=h.ComputeHash(Encoding.Unicode.GetBytes(name.ToUpperInvariant()));var s=new StringBuilder("S-1-5-80");for(int i=0;i<20;i+=4)s.Append("-"+U32(b,i));return s.ToString();}}
 public static void AssertWriters(byte[] original,string[] permitted,uint writeMask){Validate(original);var allowed=new List<string>(permitted);Need(allowed.Contains(Sid(Component(original,4,false),0)));foreach(byte[] a in Aces(Component(original,16,true),true))if(a[0]==0&&(U32(a,4)&writeMask)!=0)Need(allowed.Contains(Sid(a,8)));}
 public static int SaclCount(byte[] original){Validate(original);byte[] s=Component(original,12,true);return s==null?0:Aces(s,false).Count;}
 public static bool SaclPresent(byte[] original){Validate(original);return (U16(original,2)&16)!=0;}
 public static string[] Summary(byte[] original){Validate(original);var result=new List<string>();foreach(byte[] a in Aces(Component(original,16,true),true)){string sid=Sid(a,8),kind=sid=="S-1-5-11"?"authenticated_users":sid=="S-1-5-19"?"local_service":sid=="S-1-5-20"?"network_service":sid=="S-1-5-32-544"?"administrators":sid=="S-1-5-18"?"system":sid=="S-1-5-32-578"?"hyperv_administrators":"other";result.Add(kind+"|"+U32(a,4)+"|"+a[0]+"|"+a[1]);}return result.ToArray();}
 public static byte[] Build(byte[] original,string serviceSid,string transition,bool acknowledge,bool fullSaclVerified,string[] trustedAdmins){
  Validate(original);Need(fullSaclVerified&&serviceSid!=null&&System.Text.RegularExpressions.Regex.IsMatch(serviceSid,"\\AS-1-5-80-(?:[0-9]+-){4}[0-9]+\\z")&&serviceSid==Sid(SidBytes(serviceSid),0));Need(transition=="preserve"||transition=="protect-and-materialize-v1");Need((transition=="protect-and-materialize-v1")==acknowledge);
  var aces=Aces(Component(original,16,true),true);var counts=new Dictionary<string,int>();foreach(string sid in new[]{"S-1-5-11","S-1-5-19","S-1-5-20"})counts[sid]=0;
  var writers=new List<string>();writers.Add("S-1-5-18");writers.Add("S-1-5-32-544");writers.Add(serviceSid);Need(trustedAdmins!=null&&trustedAdmins.Length<=32);foreach(string sid in trustedAdmins){SidBytes(sid);writers.Add(sid);}Need(writers.Contains(Sid(Component(original,4,false),0)));
  foreach(byte[] a in aces){string sid=Sid(a,8);Need(sid!=serviceSid);if(counts.ContainsKey(sid)){Need(a[0]==0&&U32(a,4)==19&&(a[1]&12)==0);counts[sid]++;if((a[1]&16)!=0)Need(transition=="protect-and-materialize-v1");W32(a,4,1);}if(transition=="protect-and-materialize-v1")a[1]=(byte)(a[1]&~16);if(a[0]==0&&(U32(a,4)&0x500d001eU)!=0)Need(writers.Contains(sid));}
  foreach(int count in counts.Values)Need(count==1);
  byte[] newSid=SidBytes(serviceSid),grant=new byte[8+newSid.Length];grant[0]=0;W16(grant,2,grant.Length);W32(grant,4,0x20003);Buffer.BlockCopy(newSid,0,grant,8,newSid.Length);
  int insertion=aces.Count;for(int i=0;i<aces.Count;i++)if((aces[i][1]&16)!=0){insertion=i;break;}aces.Insert(insertion,grant);
  int size=8;foreach(byte[] a in aces)size+=a.Length;Need(size<=65535);byte[] dacl=new byte[size];dacl[0]=Component(original,16,true)[0];W16(dacl,2,size);W16(dacl,4,aces.Count);int at=8;foreach(byte[] a in aces){Buffer.BlockCopy(a,0,dacl,at,a.Length);at+=a.Length;}
  ushort control=U16(original,2);if(transition=="protect-and-materialize-v1")control|=0x1000;byte[] target=Pack(original,dacl,control);
  Need(Same(Component(original,4,false),Component(target,4,false))&&Same(Component(original,8,false),Component(target,8,false))&&Same(Component(original,12,true),Component(target,12,true)));return target;
 }
}
'@
}
function Initialize-NpNative{
 Add-Type -Language CSharp -TypeDefinition @'
using System;using System.Runtime.InteropServices;using Microsoft.Win32.SafeHandles;using System.IO;
public sealed class HaNamespaceReadV1:IDisposable {
 [StructLayout(LayoutKind.Sequential)]struct Info{public uint Attr;public System.Runtime.InteropServices.ComTypes.FILETIME C,A,W;public uint Volume,High,Low,Links,IndexHigh,IndexLow;}
 [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)]static extern SafeFileHandle CreateFile(string p,uint a,uint s,IntPtr q,uint d,uint f,IntPtr t);
 [DllImport("kernel32.dll")]static extern bool GetFileInformationByHandle(SafeFileHandle h,out Info i);
 SafeFileHandle h;public FileStream Stream;bool dir;
 public HaNamespaceReadV1(string path,bool directory){dir=directory;h=CreateFile(path,0x80000000,1,IntPtr.Zero,3,0x02200000,IntPtr.Zero);if(h.IsInvalid)throw new InvalidOperationException("namespace_input_open_failed");Info i=Read();if(((i.Attr&16)!=0)!=directory)throw new InvalidOperationException("namespace_input_type_rejected");if(!directory)Stream=new FileStream(h,FileAccess.Read,65536,false);}
 Info Read(){Info i;if(!GetFileInformationByHandle(h,out i)||(i.Attr&0x400)!=0||i.Links!=1)throw new InvalidOperationException("namespace_input_identity_rejected");return i;}
 public string Identity(){Info i=Read();return i.Volume.ToString("x8")+":"+i.IndexHigh.ToString("x8")+i.IndexLow.ToString("x8");}
 public void Dispose(){if(Stream!=null)Stream.Dispose();if(h!=null)h.Dispose();}
}
public sealed class HaNamespacePrivilegeV1:IDisposable {
 [StructLayout(LayoutKind.Sequential)]struct Luid{public uint Low;public int High;}
 [StructLayout(LayoutKind.Sequential)]struct Priv{public uint Count;public Luid Id;public uint Attributes;}
 [DllImport("advapi32.dll",SetLastError=true)]static extern bool OpenProcessToken(IntPtr p,uint a,out IntPtr t);
 [DllImport("advapi32.dll",CharSet=CharSet.Unicode,SetLastError=true)]static extern bool LookupPrivilegeValue(string system,string name,out Luid id);
 [DllImport("advapi32.dll",SetLastError=true)]static extern bool AdjustTokenPrivileges(IntPtr t,bool disable,ref Priv p,uint len,out Priv old,out uint size);
 [DllImport("kernel32.dll")]static extern bool CloseHandle(IntPtr h);
 [DllImport("advapi32.dll",CharSet=CharSet.Unicode,SetLastError=true)]static extern IntPtr OpenSCManager(string m,string db,uint access);
 [DllImport("advapi32.dll",CharSet=CharSet.Unicode,SetLastError=true)]static extern IntPtr OpenService(IntPtr m,string n,uint access);
 [DllImport("advapi32.dll",SetLastError=true)]static extern bool QueryServiceConfig2(IntPtr s,int level,IntPtr b,uint size,out uint need);
 [DllImport("advapi32.dll")]static extern bool CloseServiceHandle(IntPtr h);
 [DllImport("advapi32.dll",SetLastError=true)]static extern bool QueryServiceObjectSecurity(IntPtr service,uint info,IntPtr buffer,uint length,out uint needed);
 IntPtr token;Priv previous;bool changed;
 public HaNamespacePrivilegeV1(){if(!OpenProcessToken(System.Diagnostics.Process.GetCurrentProcess().Handle,0x28,out token))throw new InvalidOperationException("namespace_security_privilege_required");try{Luid id;if(!LookupPrivilegeValue(null,"SeSecurityPrivilege",out id))throw new InvalidOperationException("namespace_security_privilege_required");Priv next=new Priv();next.Count=1;next.Id=id;next.Attributes=2;uint size;if(!AdjustTokenPrivileges(token,false,ref next,(uint)Marshal.SizeOf(typeof(Priv)),out previous,out size)||Marshal.GetLastWin32Error()!=0)throw new InvalidOperationException("namespace_security_privilege_required");changed=true;}catch{Dispose();throw;}}
 public static uint ServiceSidType(string name){IntPtr manager=OpenSCManager(null,null,1);if(manager==IntPtr.Zero)throw new InvalidOperationException("namespace_scm_query_failed");try{IntPtr service=OpenService(manager,name,1);if(service==IntPtr.Zero)throw new InvalidOperationException("namespace_scm_query_failed");try{IntPtr b=Marshal.AllocHGlobal(4);try{uint need;if(!QueryServiceConfig2(service,5,b,4,out need)||need!=4)throw new InvalidOperationException("namespace_scm_query_failed");return (uint)Marshal.ReadInt32(b);}finally{Marshal.FreeHGlobal(b);}}finally{CloseServiceHandle(service);}}finally{CloseServiceHandle(manager);}}
 public static byte[] ServiceSecurity(string name){IntPtr manager=OpenSCManager(null,null,1);if(manager==IntPtr.Zero)throw new InvalidOperationException("namespace_scm_query_failed");try{IntPtr service=OpenService(manager,name,0x01020000);if(service==IntPtr.Zero)throw new InvalidOperationException("namespace_scm_query_failed");try{uint size;QueryServiceObjectSecurity(service,15,IntPtr.Zero,0,out size);if(size<20||size>65536)throw new InvalidOperationException("namespace_scm_query_failed");IntPtr data=Marshal.AllocHGlobal((int)size);try{uint actual;if(!QueryServiceObjectSecurity(service,15,data,size,out actual)||actual>size)throw new InvalidOperationException("namespace_scm_query_failed");byte[] bytes=new byte[actual];Marshal.Copy(data,bytes,0,(int)actual);return bytes;}finally{Marshal.FreeHGlobal(data);}}finally{CloseServiceHandle(service);}}finally{CloseServiceHandle(manager);}}
 public void Dispose(){if(token==IntPtr.Zero)return;bool failed=false;try{if(changed){Priv ignored;uint size;failed=!AdjustTokenPrivileges(token,false,ref previous,(uint)Marshal.SizeOf(typeof(Priv)),out ignored,out size)||Marshal.GetLastWin32Error()!=0;}}finally{CloseHandle(token);token=IntPtr.Zero;}if(failed)throw new InvalidOperationException("namespace_privilege_restore_failed");}
}
'@
}
function Assert-NpPrivate([string]$Path,[bool]$Root=$false){
 $item=Get-Item -LiteralPath $Path -Force;$walk=$item
 while($null -ne $walk){if($walk.Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'namespace_reparse_rejected'};if($walk -is [IO.DirectoryInfo]){$walk=$walk.Parent}elseif($walk -is [IO.FileInfo]){$walk=$walk.Directory}else{throw 'namespace_path_type_rejected'}}
 $sd=[Security.AccessControl.RawSecurityDescriptor]::new((Get-Acl -LiteralPath $Path).Sddl)
 $me=[Security.Principal.WindowsIdentity]::GetCurrent();try{$owners=@('S-1-5-18','S-1-5-32-544',$me.User.Value)}finally{$me.Dispose()}
 if($sd.Owner.Value -notin $owners -or $null -eq $sd.DiscretionaryAcl -or $sd.DiscretionaryAcl.Count -lt 2){throw 'namespace_private_owner_required'}
 $seen=@{}
 foreach($ace in $sd.DiscretionaryAcl){if($ace -isnot [Security.AccessControl.CommonAce] -or $ace.IsCallback -or $ace.AceQualifier -ne 'AccessAllowed' -or $ace.SecurityIdentifier.Value -notin @('S-1-5-18','S-1-5-32-544') -or $ace.AccessMask -ne 2032127 -or (([int]$ace.AceFlags) -band ([int][Security.AccessControl.AceFlags]::InheritOnly))){throw 'namespace_private_acl_required'};if($Root -and ((([int]$ace.AceFlags) -band 3) -ne 3)){throw 'namespace_private_inheritance_required'};$seen[$ace.SecurityIdentifier.Value]=$true}
 if($seen.Count -ne 2 -or ($Root -and ($sd.DiscretionaryAcl.Count -ne 2 -or -not ($sd.ControlFlags -band [Security.AccessControl.ControlFlags]::DiscretionaryAclProtected)))){throw 'namespace_private_acl_required'}
}
function Open-NpInput([string]$Path,[string]$Hash,[int]$Limit=65536){
 if($Hash -cnotmatch '^[0-9a-f]{64}$'){throw 'namespace_hash_rejected'};Assert-NpPrivate $Path
 $lease=[HaNamespaceReadV1]::new($Path,$false)
 try{if($lease.Stream.Length -lt 1 -or $lease.Stream.Length -gt $Limit){throw 'namespace_input_limit'};$bytes=[byte[]]::new([int]$lease.Stream.Length);$offset=0;while($offset -lt $bytes.Length){$n=$lease.Stream.Read($bytes,$offset,$bytes.Length-$offset);if($n -le 0){throw 'namespace_input_truncated'};$offset+=$n};$lease.Stream.Position=0;if((Get-NpHash $bytes) -cne $Hash){throw 'namespace_input_hash_changed'};$script:npHeld.Add($lease);return ,$bytes}catch{$lease.Dispose();throw}
}
function Assert-NpManifest($Value){
 Assert-NpKeys $Value @('schema_version','source_commit','installation_id','service_source_commit','service_name','service_sid','service_config_sha256','service_sd_sha256','service_image_sha256','service_ownership_receipt_sha256','protected_workloads_receipt_sha256','provider_semantics_receipt_sha256','original_sd_sha256','parent_sd_sha256','children_sha256','inheritance_transition','acknowledge_parent_policy_no_longer_inherited','acknowledge_namespace_wide_not_vm_specific','trusted_admin_sids')
 if(($Value.schema_version -isnot [int] -and $Value.schema_version -isnot [long]) -or $Value.schema_version -ne 1){throw 'namespace_manifest_rejected'}
 foreach($key in @('source_commit','service_source_commit')){if($Value.$key -isnot [string] -or $Value.$key -cnotmatch '^[0-9a-f]{40}$'){throw 'namespace_manifest_rejected'}}
 foreach($key in @('installation_id','service_config_sha256','service_sd_sha256','service_image_sha256','service_ownership_receipt_sha256','protected_workloads_receipt_sha256','provider_semantics_receipt_sha256','original_sd_sha256','parent_sd_sha256','children_sha256')){if($Value.$key -isnot [string] -or $Value.$key -cnotmatch '^[0-9a-f]{64}$'){throw 'namespace_manifest_rejected'}}
 if($Value.service_name -isnot [string] -or $Value.service_name -cne [HaNamespacePolicyCoreV1]::ServiceName($Value.installation_id) -or $Value.service_sid -isnot [string] -or $Value.service_sid -cne [HaNamespacePolicyCoreV1]::ServiceSid($Value.service_name)){throw 'namespace_service_binding_rejected'}
 if($Value.inheritance_transition -isnot [string] -or $Value.inheritance_transition -cnotin @('preserve','protect-and-materialize-v1') -or $Value.acknowledge_parent_policy_no_longer_inherited -isnot [bool] -or (($Value.inheritance_transition -ceq 'protect-and-materialize-v1') -ne $Value.acknowledge_parent_policy_no_longer_inherited) -or $Value.acknowledge_namespace_wide_not_vm_specific -isnot [bool] -or -not $Value.acknowledge_namespace_wide_not_vm_specific){throw 'namespace_transition_rejected'}
 if($Value.trusted_admin_sids -isnot [array] -or $Value.trusted_admin_sids.Count -lt 1 -or $Value.trusted_admin_sids.Count -gt 32){throw 'namespace_admins_rejected'}
 foreach($sid in $Value.trusted_admin_sids){if($sid -isnot [string] -or $sid -cnotmatch '^S-1-(\d+-){1,14}\d+$'){throw 'namespace_admins_rejected'}}
 if(@($Value.trusted_admin_sids|Sort-Object -Unique).Count -ne $Value.trusted_admin_sids.Count){throw 'namespace_admins_rejected'}
}
function Get-NpNamespace([string]$Namespace){
 if($Namespace -cnotin @('root/virtualization','root/virtualization/v2')){throw 'namespace_scope_rejected'}
 $scope=[System.Management.ManagementScope]::new(('\\.\'+$Namespace.Replace('/','\')));$scope.Options.EnablePrivileges=$true;$scope.Connect()
 $class=[System.Management.ManagementClass]::new($scope,[System.Management.ManagementPath]::new('__SystemSecurity'),$null)
 try{
  $result=$class.InvokeMethod('GetSD',$null,$null);if($result.ReturnValue -ne 0 -or $null -eq $result.SD){throw 'namespace_full_read_failed'};$bytes=[byte[]]$result.SD
  $full=$class.InvokeMethod('GetSecurityDescriptor',$null,$null);if($full.ReturnValue -ne 0 -or $null -eq $full.Descriptor){throw 'namespace_full_read_failed'}
  $count=if($null -eq $full.Descriptor.SACL){0}else{@($full.Descriptor.SACL).Count}
  if([HaNamespacePolicyCoreV1]::SaclPresent($bytes) -ne (([int]$full.Descriptor.ControlFlags -band 16) -ne 0) -or [HaNamespacePolicyCoreV1]::SaclCount($bytes) -ne $count){throw 'namespace_full_sacl_unverified'}
  return ,$bytes
 }finally{$class.Dispose()}
}
function Set-NpNamespace([byte[]]$Bytes){
 # The only namespace mutation primitive; caller must journal BEFORE this call.
 $scope=[System.Management.ManagementScope]::new('\\.\root\virtualization\v2');$scope.Options.EnablePrivileges=$true;$scope.Connect()
 $class=[System.Management.ManagementClass]::new($scope,[System.Management.ManagementPath]::new('__SystemSecurity'),$null)
 try{$inputParameters=$class.GetMethodParameters('SetSD');$inputParameters.SD=$Bytes;$answer=$class.InvokeMethod('SetSD',$inputParameters,$null);if($answer.ReturnValue -ne 0){throw 'namespace_set_failed_preserve_journal'}}finally{$class.Dispose()}
}
function Get-NpChildren{
 $names=@(Get-CimInstance -Namespace 'root/virtualization/v2' -ClassName '__NAMESPACE' -ErrorAction Stop|ForEach-Object{$_.Name}|Sort-Object -CaseSensitive)
 if($names.Count -gt 64 -or @($names|Sort-Object -Unique).Count -ne $names.Count){throw 'namespace_children_rejected'}
 foreach($name in $names){if($name -isnot [string] -or $name -cnotmatch '^[A-Za-z_][A-Za-z0-9_]{0,127}$'){throw 'namespace_children_rejected'}}
 return ,$names
}
function Assert-NpServiceFacts($Service,$Spec,[string]$Root,[string]$ActualSid,[uint32]$SidType){
 if($null -eq $service -or $service.State -cne 'Stopped' -or $service.ProcessId -ne 0 -or $service.StartName -ine ('NT SERVICE\'+$Spec.service_name) -or $service.ServiceType -cne 'Own Process' -or $service.DesktopInteract -or $SidType -ne 1){throw 'namespace_owned_stopped_service_required'}
 if($ActualSid -cne $Spec.service_sid){throw 'namespace_service_binding_rejected'}
 $config=[ordered]@{name=$service.Name;start_name=$service.StartName;path_name=$service.PathName;start_mode=$service.StartMode;service_type=$service.ServiceType;desktop_interact=[bool]$service.DesktopInteract}
 if((Get-NpJsonHash $config) -cne $Spec.service_config_sha256){throw 'namespace_service_config_changed'}
 if($service.PathName -cnotmatch '^"([^"\r\n]+\.exe)"( [^\r\n]*)?$'){throw 'namespace_service_path_rejected'};$image=$Matches[1]
 $base=$Root.TrimEnd('\')+'\';if(-not $image.StartsWith($base,[StringComparison]::OrdinalIgnoreCase)){throw 'namespace_existing_service_rejected'}
 return [pscustomobject]@{config=$config;image=$image}
}
function Get-NpService($Spec,[string]$Root){
 $service=Get-CimInstance Win32_Service -Filter ("Name='"+$Spec.service_name+"'") -ErrorAction Stop
 $actualSid=([Security.Principal.NTAccount]::new('NT SERVICE\'+$Spec.service_name)).Translate([Security.Principal.SecurityIdentifier]).Value
 $serviceSd=[HaNamespacePrivilegeV1]::ServiceSecurity($Spec.service_name)
 if((Get-NpHash $serviceSd) -cne $Spec.service_sd_sha256){throw 'namespace_service_sd_changed'}
 # Includes PAUSE_CONTINUE, USER_DEFINED_CONTROL and GENERIC_EXECUTE.
 [HaNamespacePolicyCoreV1]::AssertWriters($serviceSd,[string[]](@('S-1-5-18','S-1-5-32-544',$Spec.service_sid)+$Spec.trusted_admin_sids),1879900530)
 $facts=Assert-NpServiceFacts $service $Spec $Root $actualSid ([HaNamespacePrivilegeV1]::ServiceSidType($Spec.service_name));$image=$facts.image;$config=$facts.config
 $null=Open-NpInput $image $Spec.service_image_sha256 67108864
 $owned=Read-NpJson (Open-NpInput (Join-Path $Root 'namespace-service-ownership.json') $Spec.service_ownership_receipt_sha256)
 Assert-NpKeys $owned @('schema_version','format','source_commit','installation_id','service_name','service_sid','service_config_sha256','service_image_sha256','new_owned_service','scm_registration_complete')
 if(($owned.schema_version -isnot [int] -and $owned.schema_version -isnot [long]) -or $owned.schema_version -ne 1 -or $owned.format -cne 'ha-owned-controller-service-v1' -or $owned.source_commit -cne $Spec.service_source_commit -or $owned.installation_id -cne $Spec.installation_id -or $owned.service_name -cne $Spec.service_name -or $owned.service_sid -cne $Spec.service_sid -or $owned.service_config_sha256 -cne $Spec.service_config_sha256 -or $owned.service_image_sha256 -cne $Spec.service_image_sha256 -or $owned.new_owned_service -isnot [bool] -or -not $owned.new_owned_service -or $owned.scm_registration_complete -isnot [bool] -or -not $owned.scm_registration_complete){throw 'namespace_service_ownership_unverified'}
 return $config
}
function Assert-NpEvidence($Spec,[string]$Root,[string]$TargetHash,[bool]$Rollback=$false){
 $review=Read-NpJson (Open-NpInput (Join-Path $Root 'namespace-protected-workloads.json') $Spec.protected_workloads_receipt_sha256)
 Assert-NpKeys $review @('schema_version','format','installation_id','original_sd_sha256','inventory_sha256','namespace_dependency_review_sha256','workload_count','all_existing_workloads_reviewed','no_namespace_method_dependency','expires_at')
 if(($review.schema_version -isnot [int] -and $review.schema_version -isnot [long]) -or $review.schema_version -ne 1 -or $review.format -cne 'ha-namespace-protected-workloads-v1' -or $review.installation_id -cne $Spec.installation_id -or $review.original_sd_sha256 -cne $Spec.original_sd_sha256 -or ($review.workload_count -isnot [int] -and $review.workload_count -isnot [long]) -or $review.workload_count -ne 7 -or $review.all_existing_workloads_reviewed -isnot [bool] -or -not $review.all_existing_workloads_reviewed -or $review.no_namespace_method_dependency -isnot [bool] -or -not $review.no_namespace_method_dependency){throw 'namespace_protected_workloads_unverified'}
 foreach($key in @('inventory_sha256','namespace_dependency_review_sha256')){if($review.$key -isnot [string] -or $review.$key -cnotmatch '^[0-9a-f]{64}$'){throw 'namespace_protected_workloads_unverified'}}
 $expires=[DateTimeOffset]::MinValue;if($review.expires_at -isnot [string] -or -not [DateTimeOffset]::TryParseExact($review.expires_at,"yyyy-MM-dd'T'HH:mm:ss'Z'",[Globalization.CultureInfo]::InvariantCulture,[Globalization.DateTimeStyles]::AssumeUniversal,[ref]$expires) -or (-not $Rollback -and ($expires -le [DateTimeOffset]::UtcNow -or $expires -gt [DateTimeOffset]::UtcNow.AddMinutes(15)))){throw 'namespace_protected_review_expired'}
 $proof=Read-NpJson (Open-NpInput (Join-Path $Root 'namespace-provider-semantics.json') $Spec.provider_semantics_receipt_sha256)
 Assert-NpKeys $proof @('schema_version','format','installation_id','os_build','original_sd_sha256','target_sd_sha256','parent_sd_sha256','inheritance_transition','isolated_test_namespace_sha256','provider_roundtrip_exact','original_restore_exact','full_sacl_verified')
 if(($proof.schema_version -isnot [int] -and $proof.schema_version -isnot [long]) -or $proof.schema_version -ne 1 -or $proof.format -cne 'ha-namespace-provider-semantics-v1' -or $proof.installation_id -cne $Spec.installation_id -or $proof.os_build -cne [Environment]::OSVersion.Version.ToString() -or $proof.original_sd_sha256 -cne $Spec.original_sd_sha256 -or $proof.target_sd_sha256 -cne $TargetHash -or $proof.parent_sd_sha256 -cne $Spec.parent_sd_sha256 -or $proof.inheritance_transition -cne $Spec.inheritance_transition -or $proof.isolated_test_namespace_sha256 -isnot [string] -or $proof.isolated_test_namespace_sha256 -cnotmatch '^[0-9a-f]{64}$'){throw 'namespace_provider_semantics_unverified'}
 foreach($key in @('provider_roundtrip_exact','original_restore_exact','full_sacl_verified')){if($proof.$key -isnot [bool] -or -not $proof.$key){throw 'namespace_provider_semantics_unverified'}}
}
function New-NpPlan($Spec,[byte[]]$Original,[byte[]]$Current,[byte[]]$Parent,[string[]]$Children,[string[]]$Admins,[string]$ActualScriptHash){
 Assert-NpManifest $Spec
 if((Get-NpHash $Original) -cne $Spec.original_sd_sha256 -or (Get-NpHash $Parent) -cne $Spec.parent_sd_sha256 -or (Get-NpJsonHash @($Children)) -cne $Spec.children_sha256 -or (($Admins|Sort-Object)-join ',') -cne (($Spec.trusted_admin_sids|Sort-Object)-join ',')){throw 'namespace_baseline_drift'}
 if($Children.Count -ne 0){throw 'namespace_child_impact_unreviewed'}
 $target=[HaNamespacePolicyCoreV1]::Build($Original,$Spec.service_sid,$Spec.inheritance_transition,$Spec.acknowledge_parent_policy_no_longer_inherited,$true,[string[]]$Admins)
 $plan=[ordered]@{schema_version=1;format='ha-namespace-policy-b-v1';source_commit=$Spec.source_commit;script_sha256=$ActualScriptHash;installation_id=$Spec.installation_id;manifest_sha256=$ManifestSha256;namespace='root/virtualization/v2';original_sd_sha256=(Get-NpHash $Original);target_sd_sha256=(Get-NpHash $target);parent_sd_sha256=(Get-NpHash $Parent);children_sha256=(Get-NpJsonHash @($Children));child_count=$Children.Count;inheritance_transition=$Spec.inheritance_transition;acknowledge_parent_policy_no_longer_inherited=$Spec.acknowledge_parent_policy_no_longer_inherited;namespace_wide_not_vm_specific=$true;service_identity_sha256=(Get-NpHash ([Text.Encoding]::UTF8.GetBytes($Spec.service_sid)));service_config_sha256=$Spec.service_config_sha256;service_sd_sha256=$Spec.service_sd_sha256;service_ownership_receipt_sha256=$Spec.service_ownership_receipt_sha256;protected_workloads_receipt_sha256=$Spec.protected_workloads_receipt_sha256;provider_semantics_receipt_sha256=$Spec.provider_semantics_receipt_sha256;grant_mask=131075;au_ls_ns_target_mask=1;owner_group_sacl_preserved=$true;non_target_aces_preserved_except_inherited_bit=$true;provider_acceptance_proven_by_code=$false}
 return [pscustomobject]@{plan=$plan;hash=(Get-NpJsonHash $plan);target=$target;current_hash=(Get-NpHash $Current)}
}
function Write-NpRecord([string]$Path,$Value,[bool]$New){$bytes=[Text.UTF8Encoding]::new($false).GetBytes(($Value|ConvertTo-Json -Depth 12 -Compress)+"`n");$stream=[IO.File]::Open($Path,$(if($New){[IO.FileMode]::CreateNew}else{[IO.FileMode]::Append}),[IO.FileAccess]::Write,[IO.FileShare]::Read);try{$stream.Write($bytes,0,$bytes.Length);$stream.Flush($true)}finally{$stream.Dispose()}}
function Invoke-NpTransaction($Candidate,[byte[]]$Original,[string]$Root,[bool]$Rollback){
 if(-not $Go -or $PlanSha256 -cnotmatch '^[0-9a-f]{64}$' -or $Candidate.hash -cne $PlanSha256){throw 'namespace_apply_intent_required'}
 $suffix=if($Rollback){'rollback'}else{'apply'};$journal=Join-Path $Root ('namespace-policy-'+$suffix+'-journal.jsonl');$receipt=Join-Path $Root ('namespace-policy-'+$suffix+'-receipt.json')
 if((Test-Path -LiteralPath $journal) -or (Test-Path -LiteralPath $receipt)){throw 'namespace_existing_intent_manual_reconcile'}
 $before=if($Rollback){$Candidate.plan.target_sd_sha256}else{$Candidate.plan.original_sd_sha256};$after=if($Rollback){$Candidate.plan.original_sd_sha256}else{$Candidate.plan.target_sd_sha256};$bytes=if($Rollback){$Original}else{$Candidate.target}
 if($Rollback){
  $prior=Read-NpJson (Open-NpInput (Join-Path $Root 'namespace-policy-apply-receipt.json') $ApplyReceiptSha256)
  Assert-NpKeys $prior @('schema_version','format','source_commit','script_sha256','installation_id','plan_sha256','before_sd_sha256','after_sd_sha256','journal_sha256','complete','inheritance_transition','namespace_wide_not_vm_specific')
  if(($prior.schema_version -isnot [int] -and $prior.schema_version -isnot [long]) -or $prior.schema_version -ne 1 -or $prior.format -cne 'ha-namespace-policy-apply-v1' -or $prior.source_commit -cne $Candidate.plan.source_commit -or $prior.script_sha256 -cne $Candidate.plan.script_sha256 -or $prior.installation_id -cne $Candidate.plan.installation_id -or $prior.plan_sha256 -cne $PlanSha256 -or $prior.before_sd_sha256 -cne $after -or $prior.after_sd_sha256 -cne $before -or $prior.complete -isnot [bool] -or -not $prior.complete -or $prior.inheritance_transition -cne $Candidate.plan.inheritance_transition -or $prior.namespace_wide_not_vm_specific -isnot [bool] -or -not $prior.namespace_wide_not_vm_specific){throw 'namespace_rollback_binding_rejected'}
  $null=Open-NpInput (Join-Path $Root 'namespace-policy-apply-journal.jsonl') $prior.journal_sha256
 }elseif((Test-Path -LiteralPath (Join-Path $Root 'namespace-policy-rollback-journal.jsonl')) -or (Test-Path -LiteralPath (Join-Path $Root 'namespace-policy-rollback-receipt.json'))){throw 'namespace_existing_intent_manual_reconcile'}
 if((Get-NpHash (Get-NpNamespace 'root/virtualization/v2')) -cne $before){throw 'namespace_current_drift'}
 # State is acquired before one intent. Never resume/overwrite a partial intent.
 Write-NpRecord $journal ([ordered]@{sequence=1;event='namespace_set_intent';mode=$suffix;plan_sha256=$PlanSha256;source_commit=$Candidate.plan.source_commit;before_sd_sha256=$before;after_sd_sha256=$after;inheritance_transition=$Candidate.plan.inheritance_transition;namespace_wide_not_vm_specific=$true}) $true
 Set-NpNamespace $bytes
 if((Get-NpHash (Get-NpNamespace 'root/virtualization/v2')) -cne $after){throw 'namespace_readback_mismatch_preserve_journal'}
 if((Get-NpHash (Get-NpNamespace 'root/virtualization')) -cne $Candidate.plan.parent_sd_sha256 -or (Get-NpJsonHash (Get-NpChildren)) -cne $Candidate.plan.children_sha256){throw 'namespace_hierarchy_drift_preserve_journal'}
 Write-NpRecord $journal ([ordered]@{sequence=2;event='namespace_exact_readback';sd_sha256=$after}) $false
 $journalBytes=[IO.File]::ReadAllBytes($journal)
 $value=[ordered]@{schema_version=1;format=('ha-namespace-policy-'+$suffix+'-v1');source_commit=$Candidate.plan.source_commit;script_sha256=$Candidate.plan.script_sha256;installation_id=$Candidate.plan.installation_id;plan_sha256=$PlanSha256;before_sd_sha256=$before;after_sd_sha256=$after;journal_sha256=(Get-NpHash $journalBytes);complete=$true;inheritance_transition=$Candidate.plan.inheritance_transition;namespace_wide_not_vm_specific=$true}
 Write-NpRecord $receipt $value $true
 [ordered]@{scope=('namespace_policy_'+$suffix);complete=$true;plan_sha256=$PlanSha256;receipt_sha256=(Get-NpHash ([IO.File]::ReadAllBytes($receipt)));namespace_wide_not_vm_specific=$true;vm_method_effectiveness_proven=$false;existing_service_health_proven_by_code=$false;provider_actual_readback_exact=$true}|ConvertTo-Json -Compress
}
function Invoke-NamespacePolicy{
 $Mode=ConvertTo-NpMode $Mode
 if(-not $Win64DesktopAdmin -or [Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT -or -not [Environment]::Is64BitProcess -or $PSVersionTable.PSEdition -cne 'Desktop' -or $PSVersionTable.PSVersion.Major -ne 5){throw 'namespace_native_intent_required'}
 if($InstallationId -cnotmatch '^[0-9a-f]{64}$' -or $SourceCommit -cnotmatch '^[0-9a-f]{40}$' -or $ScriptSha256 -cnotmatch '^[0-9a-f]{64}$' -or (Get-NpHash ([IO.File]::ReadAllBytes($PSCommandPath))) -cne $ScriptSha256){throw 'namespace_source_binding_rejected'}
 $identity=[Security.Principal.WindowsIdentity]::GetCurrent();try{if(-not ([Security.Principal.WindowsPrincipal]::new($identity)).IsInRole([Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))){throw 'namespace_admin_required'}}finally{$identity.Dispose()}
 Assert-NpPrivate $PSCommandPath
 Initialize-NpCore;Initialize-NpNative;Add-Type -AssemblyName System.Management
 $root=Join-Path ([Environment]::GetFolderPath([Environment+SpecialFolder]::CommonApplicationData)) ('N100HA_'+$InstallationId.Substring(0,24));Assert-NpPrivate $root $true
 $script:npHeld=[Collections.Generic.List[object]]::new();$null=Open-NpInput $PSCommandPath $ScriptSha256 262144;$rootLease=[HaNamespaceReadV1]::new($root,$true);$script:npHeld.Add($rootLease);$rootIdentity=$rootLease.Identity();$rootAcl=(Get-Acl -LiteralPath $root).Sddl
 $privilege=[HaNamespacePrivilegeV1]::new()
 try{
  $current=Get-NpNamespace 'root/virtualization/v2';$parent=Get-NpNamespace 'root/virtualization';$children=Get-NpChildren
  if($Mode -ceq 'inspect'){
   [ordered]@{scope='namespace_policy_readonly_inspect';namespace='root/virtualization/v2';source_commit=$SourceCommit;sd_sha256=(Get-NpHash $current);parent_sd_sha256=(Get-NpHash $parent);children_sha256=(Get-NpJsonHash @($children));child_count=$children.Count;full_sacl_privilege_verified=$true;sacl_ace_count=[HaNamespacePolicyCoreV1]::SaclCount($current);ace_classifications=[HaNamespacePolicyCoreV1]::Summary($current);persistent_write_performed=$false;provider_semantics_verified=$false;namespace_wide_not_vm_specific=$true}|ConvertTo-Json -Depth 4 -Compress;return
  }
  if($ManifestPath -ine (Join-Path $root 'namespace-policy-request.json')){throw 'namespace_manifest_path_rejected'}
  $spec=Read-NpJson (Open-NpInput $ManifestPath $ManifestSha256);Assert-NpManifest $spec
  if($spec.source_commit -cne $SourceCommit -or $spec.installation_id -cne $InstallationId){throw 'namespace_source_binding_rejected'}
  $original=Open-NpInput (Join-Path $root 'namespace-original.bin') $spec.original_sd_sha256
  $admins=@(Get-LocalGroupMember -SID 'S-1-5-32-544' -ErrorAction Stop|ForEach-Object{$_.SID.Value}|Sort-Object)
  $candidate=New-NpPlan $spec $original $current $parent $children $admins $ScriptSha256
  $null=Get-NpService $spec $root;Assert-NpEvidence $spec $root $candidate.plan.target_sd_sha256 ($Mode -ceq 'rollback')
  if($Mode -ceq 'plan'){
   if($candidate.current_hash -cne $candidate.plan.original_sd_sha256){throw 'namespace_current_drift'}
   [ordered]@{scope='namespace_policy_readonly_plan';plan=$candidate.plan;plan_sha256=$candidate.hash;persistent_write_performed=$false;apply_performed=$false}|ConvertTo-Json -Depth 8 -Compress;return
  }
  if($rootLease.Identity() -cne $rootIdentity -or (Get-Acl -LiteralPath $root).Sddl -cne $rootAcl){throw 'namespace_root_changed'}
  Invoke-NpTransaction $candidate $original $root ($Mode -ceq 'rollback')
 }finally{try{$privilege.Dispose()}finally{foreach($held in $script:npHeld){$held.Dispose()}}}
}
if($MyInvocation.InvocationName -ne '.'){
 try{Invoke-NamespacePolicy}catch{
  # Never print WMI/RPC exception strings, descriptor data, paths or identities.
  $errorType=$_.Exception.GetType().FullName;if($errorType -cnotmatch '\A[A-Za-z][A-Za-z0-9_.+`]{0,159}\z'){$errorType='unclassified'}
  [ordered]@{scope='namespace_policy_rejected_preserve_evidence';complete=$false;automatic_retry_allowed=$false;manual_reconcile_required=$true;exception_type=$errorType;hresult=[int]$_.Exception.HResult}|ConvertTo-Json -Compress
  exit 1
 }
}
