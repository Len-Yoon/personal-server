param(
 [ValidateSet('inspect','plan','apply','rollback')][string]$Mode='inspect',
 [string]$InstallationId,[string]$SourceCommit,[string]$ScriptSha256,
 [string]$ManifestPath,[string]$ManifestSha256,[string]$PlanSha256,[string]$ApplyReceiptSha256,
 [ValidateSet('raw-exact-v1','strict-canonical-observed-v1')][string]$DescriptorProfile='raw-exact-v1',
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
function Read-NpJson([byte[]]$Bytes,[int]$Limit=65536){
 if($Bytes.Length -lt 2 -or $Bytes.Length -gt $Limit){throw 'namespace_json_limit'}
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
 static List<byte[]> Aces(byte[] acl,bool dacl){var list=new List<byte[]>();if(acl==null)return list;int count=U16(acl,4),o=8;Need(count<=128&&(acl.Length&3)==0);for(int i=0;i<count;i++){Need(o<=acl.Length-4);int n=U16(acl,o+2);Need(n>=4&&(n&3)==0&&o<=acl.Length-n);if(dacl){Need((acl[o]==0||acl[o]==1)&&n>=16&&n==8+SidSize(acl,o+8));}list.Add(Slice(acl,o,n));o+=n;}// ACL allocation may exceed bytes in use; accept only aligned, zero free space.
 Need(o<=acl.Length&&((acl.Length-o)&3)==0);for(int i=o;i<acl.Length;i++)Need(acl[i]==0);return list;}
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
  byte[] originalDacl=Component(original,16,true);var aces=Aces(originalDacl,true);int free=originalDacl.Length-8;foreach(byte[] a in aces)free-=a.Length;var counts=new Dictionary<string,int>();foreach(string sid in new[]{"S-1-5-11","S-1-5-19","S-1-5-20"})counts[sid]=0;
  var writers=new List<string>();writers.Add("S-1-5-18");writers.Add("S-1-5-32-544");writers.Add(serviceSid);Need(trustedAdmins!=null&&trustedAdmins.Length<=32);foreach(string sid in trustedAdmins){SidBytes(sid);writers.Add(sid);}Need(writers.Contains(Sid(Component(original,4,false),0)));
  foreach(byte[] a in aces){string sid=Sid(a,8);Need(sid!=serviceSid);if(counts.ContainsKey(sid)){Need(a[0]==0&&U32(a,4)==19&&(a[1]&12)==0);counts[sid]++;if((a[1]&16)!=0)Need(transition=="protect-and-materialize-v1");W32(a,4,1);}if(transition=="protect-and-materialize-v1")a[1]=(byte)(a[1]&~16);if(a[0]==0&&(U32(a,4)&0x500d001eU)!=0)Need(writers.Contains(sid));}
  foreach(int count in counts.Values)Need(count==1);
  byte[] newSid=SidBytes(serviceSid),grant=new byte[8+newSid.Length];grant[0]=0;W16(grant,2,grant.Length);W32(grant,4,0x20003);Buffer.BlockCopy(newSid,0,grant,8,newSid.Length);
  int insertion=aces.Count;for(int i=0;i<aces.Count;i++)if((aces[i][1]&16)!=0){insertion=i;break;}aces.Insert(insertion,grant);
  // Preserve the original zero free-space length; provider byte-for-byte round-trip still requires native proof.
  int size=8+free;foreach(byte[] a in aces)size+=a.Length;Need(size<=65535);byte[] dacl=new byte[size];Buffer.BlockCopy(originalDacl,0,dacl,0,8);W16(dacl,2,size);W16(dacl,4,aces.Count);int at=8;foreach(byte[] a in aces){Buffer.BlockCopy(a,0,dacl,at,a.Length);at+=a.Length;}
  ushort control=U16(original,2);if(transition=="protect-and-materialize-v1")control|=0x1000;byte[] target=Pack(original,dacl,control);
  Need(Same(Component(original,4,false),Component(target,4,false))&&Same(Component(original,8,false),Component(target,8,false))&&Same(Component(original,12,true),Component(target,12,true)));return target;
 }
}
'@
}
# Explicit opt-in only; original parser and full-SACL acquisition remain unchanged.
function Test-NpObserved($Value){return $null -ne $Value.PSObject.Properties['descriptor_profile']}
function Initialize-NpCanonical($Spec){
 if($Spec.descriptor_profile -cne 'strict-canonical-observed-v1' -or $Spec.comparator_sha256 -cne '56cc20aaf52bd7ea7b1b0f88d562a15d1f28ffb3aac7ad4b0e0df2ec3e1dac3e'){throw 'namespace_comparator_rejected'}
 $bytes=Open-NpInput (Join-Path $PSScriptRoot 'strict-canonical-sd.cs') $Spec.comparator_sha256
 if('StrictCanonicalSd' -as [type]){if(-not (Get-Variable npCanonicalInitialized -Scope Script -ErrorAction SilentlyContinue)){throw 'namespace_canonical_type_collision'}}else{Add-Type -Language CSharp -TypeDefinition ([Text.UTF8Encoding]::new($false,$true).GetString($bytes));$script:npCanonicalInitialized=$true}
}
function Get-NpCanonicalHash([byte[]]$Bytes){return Get-NpHash ([StrictCanonicalSd]::Canonicalize($Bytes))}
function Write-NpRaw([string]$Path,[byte[]]$Bytes){
 $stream=[IO.File]::Open($Path,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::Read)
 try{$stream.Write($Bytes,0,$Bytes.Length);$stream.Flush($true)}finally{$stream.Dispose()}
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
function Assert-NpObservedRootFacts([string]$Owner,[bool]$Protected,[object[]]$Aces,[string]$Installation,[bool]$HostImage=$false){
 # The reserved installation root and executable have distinct exact ACLs.
 # The service SID is derived from this installation ID.
 $serviceSid=[HaNamespacePolicyCoreV1]::ServiceSid([HaNamespacePolicyCoreV1]::ServiceName($Installation))
 if($Owner -cnotin @('S-1-5-18','S-1-5-32-544') -or -not $Protected -or $Aces.Count -ne 3){throw 'namespace_observed_root_acl_rejected'}
 $serviceMask=if($HostImage){0x1200a9}else{131232}
 $tcbFlags=if($HostImage){0}else{3}
 $seen=@{}
 foreach($a in $Aces){
  if($a.simple_allow -isnot [bool] -or -not $a.simple_allow -or $seen.ContainsKey($a.sid)){throw 'namespace_observed_root_acl_rejected'}
  if($a.sid -ceq $serviceSid){if($a.mask -ne $serviceMask -or $a.flags -ne 0){throw 'namespace_observed_root_acl_rejected'}}
  elseif($a.sid -cin @('S-1-5-18','S-1-5-32-544')){if($a.mask -ne 2032127 -or $a.flags -ne $tcbFlags){throw 'namespace_observed_root_acl_rejected'}}
  else{throw 'namespace_observed_root_acl_rejected'}
  $seen[$a.sid]=$true
 }
 if(-not $seen.ContainsKey($serviceSid) -or -not $seen.ContainsKey('S-1-5-18') -or -not $seen.ContainsKey('S-1-5-32-544')){throw 'namespace_observed_root_acl_rejected'}
}
function Assert-NpSelectedProfile($Spec,[string]$Selected){
 if($Selected -ceq 'strict-canonical-observed-v1'){
  if(-not (Test-NpObserved $Spec) -or $Spec.descriptor_profile -cne $Selected){throw 'namespace_selected_profile_mismatch'}
 }elseif($Selected -cne 'raw-exact-v1' -or (Test-NpObserved $Spec)){throw 'namespace_selected_profile_mismatch'}
}
function Assert-NpPrivate([string]$Path,[bool]$Root=$false,[bool]$ObservedRoot=$false,[string]$ObservedImageInstallation=''){
 if($ObservedRoot -and -not $Root){throw 'namespace_observed_root_scope_rejected'}
 $item=Get-Item -LiteralPath $Path -Force;$walk=$item
 while($null -ne $walk){if($walk.Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'namespace_reparse_rejected'};if($walk -is [IO.DirectoryInfo]){$walk=$walk.Parent}elseif($walk -is [IO.FileInfo]){$walk=$walk.Directory}else{throw 'namespace_path_type_rejected'}}
 $sd=[Security.AccessControl.RawSecurityDescriptor]::new((Get-Acl -LiteralPath $Path).Sddl)
 if($ObservedRoot -or $ObservedImageInstallation){
  if($ObservedImageInstallation -and ($Root -or $item -isnot [IO.FileInfo])){throw 'namespace_observed_image_type_rejected'}
  if($null -eq $sd.DiscretionaryAcl){throw 'namespace_observed_root_acl_rejected'}
  $facts=@($sd.DiscretionaryAcl|ForEach-Object{[pscustomobject]@{sid=$_.SecurityIdentifier.Value;mask=$_.AccessMask;flags=[int]$_.AceFlags;simple_allow=($_ -is [Security.AccessControl.CommonAce] -and -not $_.IsCallback -and $_.AceQualifier -eq 'AccessAllowed')}})
  Assert-NpObservedRootFacts $sd.Owner.Value ([bool]($sd.ControlFlags -band [Security.AccessControl.ControlFlags]::DiscretionaryAclProtected)) $facts $(if($ObservedImageInstallation){$ObservedImageInstallation}else{$InstallationId}) ([bool]$ObservedImageInstallation)
  return
 }
 $me=[Security.Principal.WindowsIdentity]::GetCurrent();try{$owners=@('S-1-5-18','S-1-5-32-544',$me.User.Value)}finally{$me.Dispose()}
 if($sd.Owner.Value -notin $owners -or $null -eq $sd.DiscretionaryAcl -or $sd.DiscretionaryAcl.Count -lt 2){throw 'namespace_private_owner_required'}
 $seen=@{}
 foreach($ace in $sd.DiscretionaryAcl){if($ace -isnot [Security.AccessControl.CommonAce] -or $ace.IsCallback -or $ace.AceQualifier -ne 'AccessAllowed' -or $ace.SecurityIdentifier.Value -notin @('S-1-5-18','S-1-5-32-544') -or $ace.AccessMask -ne 2032127 -or (([int]$ace.AceFlags) -band ([int][Security.AccessControl.AceFlags]::InheritOnly))){throw 'namespace_private_acl_required'};if($Root -and ((([int]$ace.AceFlags) -band 3) -ne 3)){throw 'namespace_private_inheritance_required'};$seen[$ace.SecurityIdentifier.Value]=$true}
 if($seen.Count -ne 2 -or ($Root -and ($sd.DiscretionaryAcl.Count -ne 2 -or -not ($sd.ControlFlags -band [Security.AccessControl.ControlFlags]::DiscretionaryAclProtected)))){throw 'namespace_private_acl_required'}
}
function Open-NpInput([string]$Path,[string]$Hash,[int]$Limit=65536,$ObservedImageSpec=$null){
 $installation=''
 if($null -ne $ObservedImageSpec){
  $installation=$ObservedImageSpec.installation_id
  $root=Join-Path ([Environment]::GetFolderPath([Environment+SpecialFolder]::CommonApplicationData)) ('N100HA_'+$installation.Substring(0,24))
  if(-not (Test-NpObserved $ObservedImageSpec) -or $Path -ine (Join-Path (Join-Path $root 'host') 'windows-service-host.exe')){throw 'namespace_observed_image_scope_rejected'}
 }
 if($Hash -cnotmatch '^[0-9a-f]{64}$'){throw 'namespace_hash_rejected'};Assert-NpPrivate $Path -ObservedImageInstallation $installation
 $lease=[HaNamespaceReadV1]::new($Path,$false)
 try{if($lease.Stream.Length -lt 1 -or $lease.Stream.Length -gt $Limit){throw 'namespace_input_limit'};$bytes=[byte[]]::new([int]$lease.Stream.Length);$offset=0;while($offset -lt $bytes.Length){$n=$lease.Stream.Read($bytes,$offset,$bytes.Length-$offset);if($n -le 0){throw 'namespace_input_truncated'};$offset+=$n};$lease.Stream.Position=0;if((Get-NpHash $bytes) -cne $Hash){throw 'namespace_input_hash_changed'};if($installation){Assert-NpPrivate $Path -ObservedImageInstallation $installation};$script:npHeld.Add($lease);return ,$bytes}catch{$lease.Dispose();throw}
}
function Assert-NpManifest($Value){
 $keys=@('schema_version','source_commit','installation_id','service_source_commit','service_name','service_sid','service_config_sha256','service_sd_sha256','service_image_sha256','service_ownership_receipt_sha256','protected_workloads_receipt_sha256','provider_semantics_receipt_sha256','original_sd_sha256','parent_sd_sha256','children_sha256','inheritance_transition','acknowledge_parent_policy_no_longer_inherited','acknowledge_namespace_wide_not_vm_specific','trusted_admin_sids')
 if($Value.PSObject.Properties['closure_version']){$keys+=@('closure_version','closure_names','closure_original_sd_sha256','closure_target_sd_sha256','closure_classification_sha256','closure_children_sha256');Assert-NpClosure $Value}
 if(Test-NpObserved $Value){
  $keys+=@('descriptor_profile','comparator_sha256','closure_original_canonical_sha256','closure_target_canonical_sha256')
  if(-not $Value.PSObject.Properties['closure_version'] -or $Value.schema_version -ne 3 -or $Value.descriptor_profile -cne 'strict-canonical-observed-v1' -or $Value.comparator_sha256 -cne '56cc20aaf52bd7ea7b1b0f88d562a15d1f28ffb3aac7ad4b0e0df2ec3e1dac3e'){throw 'namespace_observed_profile_rejected'}
  foreach($key in @('closure_original_canonical_sha256','closure_target_canonical_sha256')){if($Value.$key -isnot [array] -or $Value.$key.Count -ne $Value.closure_names.Count){throw 'namespace_canonical_binding_rejected'};foreach($h in $Value.$key){if($h -isnot [string] -or $h -cnotmatch '^[0-9a-f]{64}$'){throw 'namespace_canonical_binding_rejected'}}}
 }
 Assert-NpKeys $Value $keys
 if(($Value.schema_version -isnot [int] -and $Value.schema_version -isnot [long]) -or $Value.schema_version -ne $(if(Test-NpObserved $Value){3}else{1})){throw 'namespace_manifest_rejected'}
 foreach($key in @('source_commit','service_source_commit')){if($Value.$key -isnot [string] -or $Value.$key -cnotmatch '^[0-9a-f]{40}$'){throw 'namespace_manifest_rejected'}}
 foreach($key in @('installation_id','service_config_sha256','service_sd_sha256','service_image_sha256','service_ownership_receipt_sha256','protected_workloads_receipt_sha256','provider_semantics_receipt_sha256','original_sd_sha256','parent_sd_sha256','children_sha256')){if($Value.$key -isnot [string] -or $Value.$key -cnotmatch '^[0-9a-f]{64}$'){throw 'namespace_manifest_rejected'}}
 if($Value.service_name -isnot [string] -or $Value.service_name -cne [HaNamespacePolicyCoreV1]::ServiceName($Value.installation_id) -or $Value.service_sid -isnot [string] -or $Value.service_sid -cne [HaNamespacePolicyCoreV1]::ServiceSid($Value.service_name)){throw 'namespace_service_binding_rejected'}
 if($Value.inheritance_transition -isnot [string] -or $Value.inheritance_transition -cnotin @('preserve','protect-and-materialize-v1') -or $Value.acknowledge_parent_policy_no_longer_inherited -isnot [bool] -or (($Value.inheritance_transition -ceq 'protect-and-materialize-v1') -ne $Value.acknowledge_parent_policy_no_longer_inherited) -or $Value.acknowledge_namespace_wide_not_vm_specific -isnot [bool] -or -not $Value.acknowledge_namespace_wide_not_vm_specific){throw 'namespace_transition_rejected'}
 if($Value.trusted_admin_sids -isnot [array] -or $Value.trusted_admin_sids.Count -lt 1 -or $Value.trusted_admin_sids.Count -gt 32){throw 'namespace_admins_rejected'}
 foreach($sid in $Value.trusted_admin_sids){if($sid -isnot [string] -or $sid -cnotmatch '^S-1-(\d+-){1,14}\d+$'){throw 'namespace_admins_rejected'}}
 if(@($Value.trusted_admin_sids|Sort-Object -Unique).Count -ne $Value.trusted_admin_sids.Count){throw 'namespace_admins_rejected'}
}
function Get-NpNamespace([string]$Namespace){
 if($Namespace -cnotin @('root/virtualization','root/virtualization/v2') -and $Namespace -cnotmatch '^root/virtualization/v2/[a-z_][a-z0-9_]{0,63}$'){throw 'namespace_scope_rejected'}
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
function Get-NpSetReturnValue($Answer){
 if($null -eq $Answer -or $Answer.ReturnValue -isnot [uint32]){throw 'namespace_set_return_unverified_preserve_journal'}
 return $Answer.ReturnValue
}
function Set-NpNamespace([byte[]]$Bytes,[string]$Namespace='root/virtualization/v2',$Spec=$null){
 # The only namespace mutation primitive; callers journal BEFORE this call.
 if($null -eq $Spec){if($Namespace -cne 'root/virtualization/v2'){throw 'namespace_scope_rejected'}}else{Assert-NpClosure $Spec;if($Namespace -cnotin $Spec.closure_names){throw 'namespace_scope_rejected'}}
 $scope=[System.Management.ManagementScope]::new(('\\.\'+$Namespace.Replace('/','\')));$scope.Options.EnablePrivileges=$true;$scope.Options.Timeout=[TimeSpan]::FromSeconds(5);$scope.Connect()
 $class=[System.Management.ManagementClass]::new($scope,[System.Management.ManagementPath]::new('__SystemSecurity'),$null)
 try{$inputParameters=$class.GetMethodParameters('SetSD');$inputParameters.SD=$Bytes;$answer=$class.InvokeMethod('SetSD',$inputParameters,$null);$result=Get-NpSetReturnValue $answer;if($null -ne $Spec -and (Test-NpObserved $Spec)){return $result};if($result -ne 0){throw 'namespace_set_failed_preserve_journal'}}finally{$class.Dispose()}
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
 if((Test-NpObserved $Spec) -and ($service.StartMode -cne 'Manual' -or $service.PathName -ine ('"'+($base+'host\windows-service-host.exe')+'"'))){throw 'namespace_observed_service_path_rejected'}
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
 $null=Open-NpInput $image $Spec.service_image_sha256 67108864 $(if(Test-NpObserved $Spec){$Spec}else{$null})
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
 $proofKeys=@('schema_version','format','installation_id','os_build','original_sd_sha256','target_sd_sha256','parent_sd_sha256','inheritance_transition','isolated_test_namespace_sha256','provider_roundtrip_exact','original_restore_exact','full_sacl_verified')
 $flags=@('provider_roundtrip_exact','original_restore_exact','full_sacl_verified')
 if(Test-NpObserved $Spec){
  $proofKeys+=@('descriptor_profile','comparator_sha256','provider_roundtrip_canonical','original_restore_canonical')
  if($proof.descriptor_profile -cne $Spec.descriptor_profile -or $proof.comparator_sha256 -cne $Spec.comparator_sha256 -or $proof.provider_roundtrip_exact -isnot [bool] -or $proof.provider_roundtrip_exact -or $proof.original_restore_exact -isnot [bool] -or $proof.original_restore_exact){throw 'namespace_provider_exact_claim_rejected'}
  $flags=@('provider_roundtrip_canonical','original_restore_canonical','full_sacl_verified')
 }
 Assert-NpKeys $proof $proofKeys
 if(($proof.schema_version -isnot [int] -and $proof.schema_version -isnot [long]) -or $proof.schema_version -ne $(if(Test-NpObserved $Spec){2}else{1}) -or $proof.format -cne $(if(Test-NpObserved $Spec){'ha-namespace-provider-semantics-canonical-v1'}else{'ha-namespace-provider-semantics-v1'}) -or $proof.installation_id -cne $Spec.installation_id -or $proof.os_build -cne [Environment]::OSVersion.Version.ToString() -or $proof.original_sd_sha256 -cne $Spec.original_sd_sha256 -or $proof.target_sd_sha256 -cne $TargetHash -or $proof.parent_sd_sha256 -cne $Spec.parent_sd_sha256 -or $proof.inheritance_transition -cne $Spec.inheritance_transition -or $proof.isolated_test_namespace_sha256 -isnot [string] -or $proof.isolated_test_namespace_sha256 -cnotmatch '^[0-9a-f]{64}$'){throw 'namespace_provider_semantics_unverified'}
 foreach($key in $flags){if($proof.$key -isnot [bool] -or -not $proof.$key){throw 'namespace_provider_semantics_unverified'}}
}
# Flat parallel vectors keep Read-NpJson's no-escape/global-duplicate-key contract.
function Assert-NpClosure($Spec){
 if($Spec.closure_version -isnot [string] -or $Spec.closure_version -cne 'direct-children-v1' -or $Spec.inheritance_transition -cne 'protect-and-materialize-v1' -or $Spec.closure_names -isnot [array] -or $Spec.closure_names.Count -lt 2 -or $Spec.closure_names.Count -gt 9 -or $Spec.closure_names[0] -cne 'root/virtualization/v2'){throw 'namespace_closure_rejected'}
 $prior='';foreach($name in $Spec.closure_names){if($name -isnot [string] -or ($name -cne 'root/virtualization/v2' -and $name -cnotmatch '^root/virtualization/v2/[a-z_][a-z0-9_]{0,63}$') -or [string]::CompareOrdinal($prior,$name) -ge 0){throw 'namespace_closure_rejected'};$prior=$name}
 foreach($key in @('closure_original_sd_sha256','closure_target_sd_sha256','closure_classification_sha256','closure_children_sha256')){if($Spec.$key -isnot [array] -or $Spec.$key.Count -ne $Spec.closure_names.Count){throw 'namespace_closure_rejected'};foreach($hash in $Spec.$key){if($hash -isnot [string] -or $hash -cnotmatch '^[0-9a-f]{64}$'){throw 'namespace_closure_rejected'}}}
 $children=@($Spec.closure_names|Select-Object -Skip 1|ForEach-Object{($_ -split '/')[-1]});$empty=Get-NpJsonHash @()
 if($Spec.closure_original_sd_sha256[0] -cne $Spec.original_sd_sha256 -or $Spec.closure_children_sha256[0] -cne $Spec.children_sha256 -or (Get-NpJsonHash $children) -cne $Spec.children_sha256){throw 'namespace_closure_binding_rejected'}
 for($i=1;$i -lt $Spec.closure_names.Count;$i++){if($Spec.closure_children_sha256[$i] -cne $empty){throw 'namespace_closure_descendants_unreviewed'}}
}
function Get-NpClosureChildren([string]$Namespace){
 if($Namespace -cne 'root/virtualization/v2' -and $Namespace -cnotmatch '^root/virtualization/v2/[a-z_][a-z0-9_]{0,63}$'){throw 'namespace_scope_rejected'}
 $names=@(Get-CimInstance -Namespace $Namespace -ClassName '__NAMESPACE' -OperationTimeoutSec 5 -ErrorAction Stop|ForEach-Object{$_.Name}|Sort-Object -CaseSensitive)
 if($names.Count -gt 8 -or @($names|Sort-Object -Unique).Count -ne $names.Count){throw 'namespace_closure_children_rejected'}
 foreach($name in $names){if($name -isnot [string] -or $name -cnotmatch '^[a-z_][a-z0-9_]{0,63}$'){throw 'namespace_closure_children_rejected'}}
 return ,$names
}
function New-NpClosureCandidate($Spec,[byte[]]$Original,[byte[]]$Target,[string]$Root){
 Assert-NpClosure $Spec;$originals=[Collections.Generic.List[byte[]]]::new();$targets=[Collections.Generic.List[byte[]]]::new()
 for($i=0;$i -lt $Spec.closure_names.Count;$i++){
  $originalBytes=if($i -eq 0){$Original}else{Open-NpInput (Join-Path $Root ('namespace-child-'+$i+'-original.bin')) $Spec.closure_original_sd_sha256[$i]}
  $targetBytes=if($i -eq 0){$Target}else{[HaNamespacePolicyCoreV1]::Build($originalBytes,$Spec.service_sid,'protect-and-materialize-v1',$true,$true,[string[]]$Spec.trusted_admin_sids)}
  if((Get-NpHash $originalBytes) -cne $Spec.closure_original_sd_sha256[$i] -or (Get-NpHash $targetBytes) -cne $Spec.closure_target_sd_sha256[$i]){throw 'namespace_closure_sd_binding_rejected'}
  # Repository/SD writers are independently checked; namespace method rights are transformed by Build.
  [HaNamespacePolicyCoreV1]::AssertWriters($originalBytes,[string[]](@('S-1-5-18','S-1-5-32-544')+@($Spec.trusted_admin_sids)),[uint32]4027318284)
  $evidence=Read-NpJson (Open-NpInput (Join-Path $Root ('namespace-closure-'+$i+'-evidence.json')) $Spec.closure_classification_sha256[$i])
  $evidenceKeys=@('schema_version','format','source_commit','installation_id','namespace','original_sd_sha256','target_sd_sha256','children_sha256','classification_metadata_sha256','protected_workloads_receipt_sha256','provider_roundtrip_exact','original_restore_exact','full_sacl_verified','control_path_review_complete','namespace_wide_not_vm_specific')
  $flags=@('provider_roundtrip_exact','original_restore_exact','full_sacl_verified','control_path_review_complete','namespace_wide_not_vm_specific')
  if(Test-NpObserved $Spec){
   $evidenceKeys+=@('descriptor_profile','comparator_sha256','provider_roundtrip_canonical','original_restore_canonical')
   if($evidence.descriptor_profile -cne $Spec.descriptor_profile -or $evidence.comparator_sha256 -cne $Spec.comparator_sha256 -or $evidence.provider_roundtrip_exact -isnot [bool] -or $evidence.provider_roundtrip_exact -or $evidence.original_restore_exact -isnot [bool] -or $evidence.original_restore_exact){throw 'namespace_closure_exact_claim_rejected'}
   if((Get-NpCanonicalHash $originalBytes) -cne $Spec.closure_original_canonical_sha256[$i] -or (Get-NpCanonicalHash $targetBytes) -cne $Spec.closure_target_canonical_sha256[$i]){throw 'namespace_closure_canonical_binding_rejected'}
   $flags=@('provider_roundtrip_canonical','original_restore_canonical','full_sacl_verified','control_path_review_complete','namespace_wide_not_vm_specific')
  }
  Assert-NpKeys $evidence $evidenceKeys
  if(($evidence.schema_version -isnot [int] -and $evidence.schema_version -isnot [long]) -or $evidence.schema_version -ne $(if(Test-NpObserved $Spec){2}else{1}) -or $evidence.format -cne $(if(Test-NpObserved $Spec){'ha-namespace-closure-evidence-canonical-v1'}else{'ha-namespace-closure-evidence-v1'}) -or $evidence.source_commit -cne $Spec.source_commit -or $evidence.installation_id -cne $Spec.installation_id -or $evidence.namespace -cne $Spec.closure_names[$i] -or $evidence.original_sd_sha256 -cne $Spec.closure_original_sd_sha256[$i] -or $evidence.target_sd_sha256 -cne $Spec.closure_target_sd_sha256[$i] -or $evidence.children_sha256 -cne $Spec.closure_children_sha256[$i] -or $evidence.protected_workloads_receipt_sha256 -cne $Spec.protected_workloads_receipt_sha256 -or $evidence.classification_metadata_sha256 -isnot [string] -or $evidence.classification_metadata_sha256 -cnotmatch '^[0-9a-f]{64}$'){throw 'namespace_closure_evidence_rejected'}
  foreach($key in $flags){if($evidence.$key -isnot [bool] -or -not $evidence.$key){throw 'namespace_closure_evidence_rejected'}}
  $originals.Add($originalBytes);$targets.Add($targetBytes)
 }
 return [pscustomobject]@{spec=$Spec;originals=$originals;targets=$targets}
}
function Assert-NpClosureState($Candidate,[string[]]$Expected){
 $spec=$Candidate.closure.spec
 if((Get-NpHash (Get-NpNamespace 'root/virtualization')) -cne $Candidate.plan.parent_sd_sha256){throw 'namespace_closure_parent_drift'}
 for($i=0;$i -lt $spec.closure_names.Count;$i++){
  if((Get-NpJsonHash (Get-NpClosureChildren $spec.closure_names[$i])) -cne $spec.closure_children_sha256[$i] -or (Get-NpHash (Get-NpNamespace $spec.closure_names[$i])) -cne $Expected[$i]){throw 'namespace_closure_live_drift_manual_reconcile'}
 }
}
function Set-NpClosureNamespace([string]$Namespace,[byte[]]$Bytes,$Spec){
 Assert-NpClosure $Spec;if($Namespace -cnotin $Spec.closure_names){throw 'namespace_scope_rejected'}
 Set-NpNamespace $Bytes $Namespace $Spec
}
function Invoke-NpClosureTransaction($Candidate,[string]$Root,[bool]$Rollback){
 if(-not $Go -or $PlanSha256 -cnotmatch '^[0-9a-f]{64}$' -or $Candidate.hash -cne $PlanSha256){throw 'namespace_apply_intent_required'}
 $spec=$Candidate.closure.spec;$suffix=if($Rollback){'rollback'}else{'apply'};$journal=Join-Path $Root ('namespace-policy-'+$suffix+'-journal.jsonl');$receipt=Join-Path $Root ('namespace-policy-'+$suffix+'-receipt.json')
 if((Test-Path -LiteralPath $journal) -or (Test-Path -LiteralPath $receipt)){throw 'namespace_existing_intent_manual_reconcile'}
 $expected=@($spec.closure_original_sd_sha256);$destination=@($spec.closure_target_sd_sha256);$order=@(1..($spec.closure_names.Count-1))+@(0)
 if($Rollback){
  $prior=Read-NpJson (Open-NpInput (Join-Path $Root 'namespace-policy-apply-receipt.json') $ApplyReceiptSha256)
  Assert-NpKeys $prior @('schema_version','format','source_commit','script_sha256','installation_id','plan_sha256','closure_names','before_sd_sha256','after_sd_sha256','journal_sha256','complete','atomic_tree_change','manual_reconcile_on_partial')
  if(($prior.schema_version -isnot [int] -and $prior.schema_version -isnot [long]) -or $prior.schema_version -ne 2 -or $prior.format -cne 'ha-namespace-closure-apply-v1' -or $prior.source_commit -cne $Candidate.plan.source_commit -or $prior.script_sha256 -cne $Candidate.plan.script_sha256 -or $prior.installation_id -cne $Candidate.plan.installation_id -or $prior.plan_sha256 -cne $PlanSha256 -or $prior.complete -isnot [bool] -or -not $prior.complete -or $prior.atomic_tree_change -isnot [bool] -or $prior.atomic_tree_change -or $prior.manual_reconcile_on_partial -isnot [bool] -or -not $prior.manual_reconcile_on_partial -or (Get-NpJsonHash @($prior.closure_names)) -cne (Get-NpJsonHash @($spec.closure_names)) -or (Get-NpJsonHash @($prior.before_sd_sha256)) -cne (Get-NpJsonHash @($expected)) -or (Get-NpJsonHash @($prior.after_sd_sha256)) -cne (Get-NpJsonHash @($destination))){throw 'namespace_rollback_binding_rejected'}
  $null=Open-NpInput (Join-Path $Root 'namespace-policy-apply-journal.jsonl') $prior.journal_sha256
  $expected=@($spec.closure_target_sd_sha256);$destination=@($spec.closure_original_sd_sha256);[array]::Reverse($order)
 }elseif((Test-Path -LiteralPath (Join-Path $Root 'namespace-policy-rollback-journal.jsonl')) -or (Test-Path -LiteralPath (Join-Path $Root 'namespace-policy-rollback-receipt.json'))){throw 'namespace_existing_intent_manual_reconcile'}
 Assert-NpClosureState $Candidate $expected
 Write-NpRecord $journal ([ordered]@{sequence=1;event='namespace_closure_intent';mode=$suffix;plan_sha256=$PlanSha256;source_commit=$Candidate.plan.source_commit;atomic_tree_change=$false;manual_reconcile_on_partial=$true}) $true
 $sequence=1
 foreach($i in $order){
  Assert-NpClosureState $Candidate $expected
  $sequence++;Write-NpRecord $journal ([ordered]@{sequence=$sequence;event='namespace_closure_set_intent';index=$i;before_sd_sha256=$expected[$i];after_sd_sha256=$destination[$i]}) $false
  $bytes=if($Rollback){$Candidate.closure.originals[$i]}else{$Candidate.closure.targets[$i]}
  Set-NpClosureNamespace $spec.closure_names[$i] $bytes $spec
  $expected[$i]=$destination[$i];Assert-NpClosureState $Candidate $expected
  $sequence++;Write-NpRecord $journal ([ordered]@{sequence=$sequence;event='namespace_closure_exact_readback';index=$i;sd_sha256=$expected[$i]}) $false
 }
 $before=if($Rollback){@($spec.closure_target_sd_sha256)}else{@($spec.closure_original_sd_sha256)}
 $value=[ordered]@{schema_version=2;format=('ha-namespace-closure-'+$suffix+'-v1');source_commit=$Candidate.plan.source_commit;script_sha256=$Candidate.plan.script_sha256;installation_id=$Candidate.plan.installation_id;plan_sha256=$PlanSha256;closure_names=@($spec.closure_names);before_sd_sha256=$before;after_sd_sha256=$destination;journal_sha256=(Get-NpHash ([IO.File]::ReadAllBytes($journal)));complete=$true;atomic_tree_change=$false;manual_reconcile_on_partial=$true}
 Write-NpRecord $receipt $value $true
 [ordered]@{scope=('namespace_closure_'+$suffix);complete=$true;plan_sha256=$PlanSha256;receipt_sha256=(Get-NpHash ([IO.File]::ReadAllBytes($receipt)));atomic_tree_change=$false;vm_method_effectiveness_proven=$false}|ConvertTo-Json -Compress
}

function Assert-NpObservedReceipt($Prior,$Candidate){
 $s=$Candidate.closure.spec
 Assert-NpKeys $Prior @('schema_version','format','source_commit','script_sha256','installation_id','plan_sha256','descriptor_profile','comparator_sha256','service_identity_sha256','service_config_sha256','service_sd_sha256','service_ownership_receipt_sha256','parent_sd_sha256','closure_names','children_sha256','classification_sha256','original_sd_sha256','original_canonical_sha256','requested_sd_sha256','requested_canonical_sha256','observed_sd_sha256','observed_canonical_sha256','original_sd_base64','requested_sd_base64','observed_sd_base64','journal_sha256','complete','atomic_tree_change','manual_reconcile_on_partial')
 if(($Prior.schema_version -isnot [int] -and $Prior.schema_version -isnot [long]) -or $Prior.schema_version -ne 3 -or $Prior.format -cne 'ha-namespace-closure-apply-observed-v1' -or $Prior.complete -isnot [bool] -or -not $Prior.complete -or $Prior.atomic_tree_change -isnot [bool] -or $Prior.atomic_tree_change -or $Prior.manual_reconcile_on_partial -isnot [bool] -or -not $Prior.manual_reconcile_on_partial){throw 'namespace_observed_receipt_incomplete'}
 foreach($key in @('source_commit','script_sha256','installation_id','descriptor_profile','comparator_sha256','service_identity_sha256','service_config_sha256','service_sd_sha256','service_ownership_receipt_sha256','parent_sd_sha256')){if($Prior.$key -cne $Candidate.plan[$key]){throw 'namespace_observed_receipt_binding_rejected'}}
 if($Prior.plan_sha256 -cne $Candidate.hash){throw 'namespace_observed_receipt_binding_rejected'}
 foreach($pair in @(@('closure_names','closure_names'),@('children_sha256','closure_children_sha256'),@('classification_sha256','closure_classification_sha256'),@('original_sd_sha256','closure_original_sd_sha256'),@('original_canonical_sha256','closure_original_canonical_sha256'),@('requested_sd_sha256','closure_target_sd_sha256'),@('requested_canonical_sha256','closure_target_canonical_sha256'))){if((Get-NpJsonHash @($Prior.($pair[0]))) -cne (Get-NpJsonHash @($s.($pair[1])))){throw 'namespace_observed_receipt_binding_rejected'}}
 foreach($key in @('observed_sd_sha256','observed_canonical_sha256','original_sd_base64','requested_sd_base64','observed_sd_base64')){if($Prior.$key -isnot [array] -or $Prior.$key.Count -ne $s.closure_names.Count){throw 'namespace_observed_receipt_binding_rejected'}}
 for($i=0;$i -lt $s.closure_names.Count;$i++){
  foreach($kind in @('original','requested','observed')){
   $bytes=[Convert]::FromBase64String($Prior.($kind+'_sd_base64')[$i])
   if((Get-NpHash $bytes) -cne $Prior.($kind+'_sd_sha256')[$i] -or (Get-NpCanonicalHash $bytes) -cne $Prior.($kind+'_canonical_sha256')[$i]){throw 'namespace_observed_receipt_raw_rejected'}
  }
  if($Prior.observed_canonical_sha256[$i] -cne $s.closure_target_canonical_sha256[$i]){throw 'namespace_observed_receipt_canonical_rejected'}
 }
 if($Prior.journal_sha256 -isnot [string] -or $Prior.journal_sha256 -cnotmatch '^[0-9a-f]{64}$'){throw 'namespace_observed_receipt_journal_rejected'}
}
function Assert-NpObservedJournalRow($Row,$Expected){
 Assert-NpKeys $Row @($Expected.Keys)
 foreach($key in $Expected.Keys){
  $actual=$Row.$key
  $wanted=$Expected[$key]
  $valid=if($wanted -is [int]){$actual -is [int] -or $actual -is [long]}elseif($wanted -is [bool]){$actual -is [bool]}else{$actual -is [string]}
  if(-not $valid -or $actual -cne $wanted){throw 'namespace_observed_journal_sequence_rejected'}
 }
}
function Assert-NpObservedApplyJournal([byte[]]$Bytes,$Prior){
 $journal=[Text.UTF8Encoding]::new($false,$true).GetString($Bytes)
 if(-not $journal.EndsWith("`n")){throw 'namespace_observed_journal_truncated'}
 $lines=$journal.Substring(0,$journal.Length-1).Split("`n")
 $count=$Prior.closure_names.Count
 if($lines.Count -ne (1+3*$count)){throw 'namespace_observed_journal_incomplete'}
 $rows=@(foreach($line in $lines){Read-NpJson ([Text.Encoding]::UTF8.GetBytes($line))})
 Assert-NpObservedJournalRow $rows[0] @{sequence=1;event='namespace_closure_intent';mode='apply';plan_sha256=$Prior.plan_sha256;source_commit=$Prior.source_commit;descriptor_profile=$Prior.descriptor_profile;comparator_sha256=$Prior.comparator_sha256;atomic_tree_change=$false;manual_reconcile_on_partial=$true}
 $offset=1
 foreach($index in (@(1..($count-1))+@(0))){
  Assert-NpObservedJournalRow $rows[$offset] @{sequence=($offset+1);event='namespace_closure_set_intent';index=$index;before_sd_sha256=$Prior.original_sd_sha256[$index];requested_sd_sha256=$Prior.requested_sd_sha256[$index];requested_canonical_sha256=$Prior.requested_canonical_sha256[$index]}
  Assert-NpObservedJournalRow $rows[($offset+1)] @{sequence=($offset+2);event='namespace_closure_set_return';index=$index;return_value=0}
  Assert-NpObservedJournalRow $rows[($offset+2)] @{sequence=($offset+3);event='namespace_closure_observed_readback';index=$index;observed_sd_sha256=$Prior.observed_sd_sha256[$index];observed_canonical_sha256=$Prior.observed_canonical_sha256[$index];full_sacl_verified=$true}
  $offset+=3
 }
}
function Invoke-NpObservedTransaction($Candidate,[string]$Root,[bool]$Rollback){
 if(-not $Go -or $PlanSha256 -cnotmatch '^[0-9a-f]{64}$' -or $Candidate.hash -cne $PlanSha256){throw 'namespace_apply_intent_required'}
 $s=$Candidate.closure.spec;$suffix=if($Rollback){'rollback'}else{'apply'}
 $journal=Join-Path $Root ('namespace-policy-'+$suffix+'-journal.jsonl');$receipt=Join-Path $Root ('namespace-policy-'+$suffix+'-receipt.json')
 if((Test-Path -LiteralPath $journal) -or (Test-Path -LiteralPath $receipt) -or (-not $Rollback -and (Test-Path -LiteralPath (Join-Path $Root 'namespace-runtime-observed.json')))){throw 'namespace_existing_intent_manual_reconcile'}
 $expected=@($s.closure_original_sd_sha256);$requested=@($s.closure_target_sd_sha256);$canonical=@($s.closure_target_canonical_sha256);$order=@(1..($s.closure_names.Count-1))+@(0)
 if($Rollback){
  $prior=Read-NpJson (Open-NpInput (Join-Path $Root 'namespace-policy-apply-receipt.json') $ApplyReceiptSha256 3145728) 3145728
  Assert-NpObservedReceipt $prior $Candidate
  $applyJournal=Open-NpInput (Join-Path $Root 'namespace-policy-apply-journal.jsonl') $prior.journal_sha256 3145728
  Assert-NpObservedApplyJournal $applyJournal $prior
  $expected=@($prior.observed_sd_sha256);$requested=@($s.closure_original_sd_sha256);$canonical=@($s.closure_original_canonical_sha256);[array]::Reverse($order)
 }elseif((Test-Path -LiteralPath (Join-Path $Root 'namespace-policy-rollback-journal.jsonl')) -or (Test-Path -LiteralPath (Join-Path $Root 'namespace-policy-rollback-receipt.json'))){throw 'namespace_existing_intent_manual_reconcile'}
 Assert-NpClosureState $Candidate $expected
 # Each raw payload is persisted before the first namespace mutation. CreateNew
 # and the journal prevent resuming or overwriting an uncertain partial apply.
 for($i=0;$i -lt $s.closure_names.Count;$i++){
  $bytes=if($Rollback){$Candidate.closure.originals[$i]}else{$Candidate.closure.targets[$i]}
  Write-NpRaw (Join-Path $Root ('namespace-'+$suffix+'-'+$i+'-requested.bin')) $bytes
 }
 Write-NpRecord $journal ([ordered]@{sequence=1;event='namespace_closure_intent';mode=$suffix;plan_sha256=$PlanSha256;source_commit=$Candidate.plan.source_commit;descriptor_profile=$s.descriptor_profile;comparator_sha256=$s.comparator_sha256;atomic_tree_change=$false;manual_reconcile_on_partial=$true}) $true
 $sequence=1;$observed=@('')*$s.closure_names.Count;$observedCanonical=@('')*$s.closure_names.Count;$observedBase64=@('')*$s.closure_names.Count
 foreach($i in $order){
  Assert-NpClosureState $Candidate $expected
  $bytes=if($Rollback){$Candidate.closure.originals[$i]}else{$Candidate.closure.targets[$i]}
  $sequence++;Write-NpRecord $journal ([ordered]@{sequence=$sequence;event='namespace_closure_set_intent';index=$i;before_sd_sha256=$expected[$i];requested_sd_sha256=$requested[$i];requested_canonical_sha256=$canonical[$i]}) $false
  $result=Set-NpClosureNamespace $s.closure_names[$i] $bytes $s
  if($result -isnot [uint32]){throw 'namespace_set_return_unverified_preserve_journal'}
  $sequence++;Write-NpRecord $journal ([ordered]@{sequence=$sequence;event='namespace_closure_set_return';index=$i;return_value=$result}) $false
  if($result -ne 0){throw 'namespace_set_failed_preserve_journal'}
  # Get-NpNamespace always performs full-SACL verification, including rollback.
  $readback=Get-NpNamespace $s.closure_names[$i]
  Write-NpRaw (Join-Path $Root ('namespace-'+$suffix+'-'+$i+'-observed.bin')) $readback
  $observed[$i]=Get-NpHash $readback;$observedCanonical[$i]=Get-NpCanonicalHash $readback;$observedBase64[$i]=[Convert]::ToBase64String($readback)
  if($observedCanonical[$i] -cne $canonical[$i]){throw 'namespace_canonical_readback_mismatch_preserve_journal'}
  # All subsequent preconditions use the just-observed RAW hash. Canonical
  # equality is limited to this one request/readback boundary.
  $expected[$i]=$observed[$i]
  $sequence++;Write-NpRecord $journal ([ordered]@{sequence=$sequence;event='namespace_closure_observed_readback';index=$i;observed_sd_sha256=$observed[$i];observed_canonical_sha256=$observedCanonical[$i];full_sacl_verified=$true}) $false
  Assert-NpClosureState $Candidate $expected
 }
 Assert-NpClosureState $Candidate $expected
 $value=[ordered]@{schema_version=3;format=('ha-namespace-closure-'+$suffix+'-observed-v1');source_commit=$Candidate.plan.source_commit;script_sha256=$Candidate.plan.script_sha256;installation_id=$s.installation_id;plan_sha256=$PlanSha256;descriptor_profile=$s.descriptor_profile;comparator_sha256=$s.comparator_sha256;service_identity_sha256=$Candidate.plan.service_identity_sha256;service_config_sha256=$s.service_config_sha256;service_sd_sha256=$s.service_sd_sha256;service_ownership_receipt_sha256=$s.service_ownership_receipt_sha256;parent_sd_sha256=$s.parent_sd_sha256;closure_names=@($s.closure_names);children_sha256=@($s.closure_children_sha256);classification_sha256=@($s.closure_classification_sha256);original_sd_sha256=@($s.closure_original_sd_sha256);original_canonical_sha256=@($s.closure_original_canonical_sha256);requested_sd_sha256=$requested;requested_canonical_sha256=$canonical;observed_sd_sha256=$observed;observed_canonical_sha256=$observedCanonical;original_sd_base64=@($Candidate.closure.originals|ForEach-Object{[Convert]::ToBase64String($_)});requested_sd_base64=@($(if($Rollback){$Candidate.closure.originals}else{$Candidate.closure.targets})|ForEach-Object{[Convert]::ToBase64String($_)});observed_sd_base64=$observedBase64;journal_sha256=(Get-NpHash ([IO.File]::ReadAllBytes($journal)));complete=$true;atomic_tree_change=$false;manual_reconcile_on_partial=$true}
 Write-NpRecord $receipt $value $true
 $hash=Get-NpHash ([IO.File]::ReadAllBytes($receipt))
 if(-not $Rollback){
  # Immutable second-phase input, never rewrite the pre-apply plan. Runtime
  # uses observed raw pins and requires this complete receipt at provisioning.
  $runtime=[ordered]@{version='direct-children-observed-v1';names=@($s.closure_names);sd_sha256=$observed;children_sha256=@($s.closure_children_sha256);classification_sha256=@($s.closure_classification_sha256);descriptor_profile=$s.descriptor_profile;comparator_sha256=$s.comparator_sha256;apply_receipt_sha256=$hash;plan_sha256=$PlanSha256;service_identity_sha256=$Candidate.plan.service_identity_sha256}
  Write-NpRecord (Join-Path $Root 'namespace-runtime-observed.json') $runtime $true
 }
 [ordered]@{scope=('namespace_closure_'+$suffix);complete=$true;plan_sha256=$PlanSha256;receipt_sha256=$hash;atomic_tree_change=$false;vm_method_effectiveness_proven=$false}|ConvertTo-Json -Compress
}

function New-NpPlan($Spec,[byte[]]$Original,[byte[]]$Current,[byte[]]$Parent,[string[]]$Children,[string[]]$Admins,[string]$ActualScriptHash){
 Assert-NpManifest $Spec
 if(Test-NpObserved $Spec){Initialize-NpCanonical $Spec}
 if((Get-NpHash $Original) -cne $Spec.original_sd_sha256 -or (Get-NpHash $Parent) -cne $Spec.parent_sd_sha256 -or (Get-NpJsonHash @($Children)) -cne $Spec.children_sha256 -or (($Admins|Sort-Object)-join ',') -cne (($Spec.trusted_admin_sids|Sort-Object)-join ',')){throw 'namespace_baseline_drift'}
 if($Children.Count -ne 0 -and -not $Spec.PSObject.Properties['closure_version']){throw 'namespace_child_impact_unreviewed'}
 $target=[HaNamespacePolicyCoreV1]::Build($Original,$Spec.service_sid,$Spec.inheritance_transition,$Spec.acknowledge_parent_policy_no_longer_inherited,$true,[string[]]$Admins)
 $plan=[ordered]@{schema_version=1;format='ha-namespace-policy-b-v1';source_commit=$Spec.source_commit;script_sha256=$ActualScriptHash;installation_id=$Spec.installation_id;manifest_sha256=$ManifestSha256;namespace='root/virtualization/v2';original_sd_sha256=(Get-NpHash $Original);target_sd_sha256=(Get-NpHash $target);parent_sd_sha256=(Get-NpHash $Parent);children_sha256=(Get-NpJsonHash @($Children));child_count=$Children.Count;inheritance_transition=$Spec.inheritance_transition;acknowledge_parent_policy_no_longer_inherited=$Spec.acknowledge_parent_policy_no_longer_inherited;namespace_wide_not_vm_specific=$true;service_identity_sha256=(Get-NpHash ([Text.Encoding]::UTF8.GetBytes($Spec.service_sid)));service_config_sha256=$Spec.service_config_sha256;service_sd_sha256=$Spec.service_sd_sha256;service_ownership_receipt_sha256=$Spec.service_ownership_receipt_sha256;protected_workloads_receipt_sha256=$Spec.protected_workloads_receipt_sha256;provider_semantics_receipt_sha256=$Spec.provider_semantics_receipt_sha256;grant_mask=131075;au_ls_ns_target_mask=1;owner_group_sacl_preserved=$true;non_target_aces_preserved_except_inherited_bit=$true;provider_acceptance_proven_by_code=$false}
 $candidate=[pscustomobject]@{plan=$plan;hash='';target=$target;current_hash=(Get-NpHash $Current)}
 if($Spec.PSObject.Properties['closure_version']){
  $closure=New-NpClosureCandidate $Spec $Original $target $script:npPlanRoot
  foreach($key in @('closure_version','closure_names','closure_original_sd_sha256','closure_target_sd_sha256','closure_classification_sha256','closure_children_sha256')){$plan[$key]=$Spec.$key}
  $candidate|Add-Member NoteProperty closure $closure
  if(Test-NpObserved $Spec){
   $plan.schema_version=3;$plan.format='ha-namespace-policy-observed-v1'
   foreach($key in @('descriptor_profile','comparator_sha256','closure_original_canonical_sha256','closure_target_canonical_sha256')){$plan[$key]=$Spec.$key}
  }else{$plan.runtime_namespace_closure=[ordered]@{version='direct-children-v1';names=@($Spec.closure_names);sd_sha256=@($Spec.closure_target_sd_sha256);children_sha256=@($Spec.closure_children_sha256);classification_sha256=@($Spec.closure_classification_sha256)}}
 }
 $candidate.hash=Get-NpJsonHash $plan;return $candidate
}
function Write-NpRecord([string]$Path,$Value,[bool]$New){$bytes=[Text.UTF8Encoding]::new($false).GetBytes(($Value|ConvertTo-Json -Depth 12 -Compress)+"`n");$stream=[IO.File]::Open($Path,$(if($New){[IO.FileMode]::CreateNew}else{[IO.FileMode]::Append}),[IO.FileAccess]::Write,[IO.FileShare]::Read);try{$stream.Write($bytes,0,$bytes.Length);$stream.Flush($true)}finally{$stream.Dispose()}}
function Invoke-NpTransaction($Candidate,[byte[]]$Original,[string]$Root,[bool]$Rollback){
 if($Candidate.PSObject.Properties['closure'] -and (Test-NpObserved $Candidate.closure.spec)){Invoke-NpObservedTransaction $Candidate $Root $Rollback;return}
 if($Candidate.PSObject.Properties['closure']){Invoke-NpClosureTransaction $Candidate $Root $Rollback;return}
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
 $DescriptorProfile=$DescriptorProfile.ToLowerInvariant()
 if(-not $Win64DesktopAdmin -or [Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT -or -not [Environment]::Is64BitProcess -or $PSVersionTable.PSEdition -cne 'Desktop' -or $PSVersionTable.PSVersion.Major -ne 5){throw 'namespace_native_intent_required'}
 if($InstallationId -cnotmatch '^[0-9a-f]{64}$' -or $SourceCommit -cnotmatch '^[0-9a-f]{40}$' -or $ScriptSha256 -cnotmatch '^[0-9a-f]{64}$' -or (Get-NpHash ([IO.File]::ReadAllBytes($PSCommandPath))) -cne $ScriptSha256){throw 'namespace_source_binding_rejected'}
 $identity=[Security.Principal.WindowsIdentity]::GetCurrent();try{if(-not ([Security.Principal.WindowsPrincipal]::new($identity)).IsInRole([Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))){throw 'namespace_admin_required'}}finally{$identity.Dispose()}
 Assert-NpPrivate $PSCommandPath
 Initialize-NpCore;Initialize-NpNative;Add-Type -AssemblyName System.Management
 $root=Join-Path ([Environment]::GetFolderPath([Environment+SpecialFolder]::CommonApplicationData)) ('N100HA_'+$InstallationId.Substring(0,24));Assert-NpPrivate $root $true ($DescriptorProfile -ceq 'strict-canonical-observed-v1')
 $script:npHeld=[Collections.Generic.List[object]]::new();$null=Open-NpInput $PSCommandPath $ScriptSha256 262144;$rootLease=[HaNamespaceReadV1]::new($root,$true);$script:npHeld.Add($rootLease);$rootIdentity=$rootLease.Identity();$rootAcl=(Get-Acl -LiteralPath $root).Sddl
 $privilege=[HaNamespacePrivilegeV1]::new()
 try{
  $current=Get-NpNamespace 'root/virtualization/v2';$parent=Get-NpNamespace 'root/virtualization';$children=Get-NpChildren
  if($Mode -ceq 'inspect'){
   [ordered]@{scope='namespace_policy_readonly_inspect';namespace='root/virtualization/v2';source_commit=$SourceCommit;sd_sha256=(Get-NpHash $current);parent_sd_sha256=(Get-NpHash $parent);children_sha256=(Get-NpJsonHash @($children));child_count=$children.Count;full_sacl_privilege_verified=$true;sacl_ace_count=[HaNamespacePolicyCoreV1]::SaclCount($current);ace_classifications=[HaNamespacePolicyCoreV1]::Summary($current);persistent_write_performed=$false;provider_semantics_verified=$false;namespace_wide_not_vm_specific=$true}|ConvertTo-Json -Depth 4 -Compress;return
  }
  if($ManifestPath -ine (Join-Path $root 'namespace-policy-request.json')){throw 'namespace_manifest_path_rejected'}
  $spec=Read-NpJson (Open-NpInput $ManifestPath $ManifestSha256);Assert-NpManifest $spec;Assert-NpSelectedProfile $spec $DescriptorProfile
  if($spec.source_commit -cne $SourceCommit -or $spec.installation_id -cne $InstallationId){throw 'namespace_source_binding_rejected'}
  $original=Open-NpInput (Join-Path $root 'namespace-original.bin') $spec.original_sd_sha256
  $admins=@(Get-LocalGroupMember -SID 'S-1-5-32-544' -ErrorAction Stop|ForEach-Object{$_.SID.Value}|Sort-Object)
  $script:npPlanRoot=$root
  $candidate=New-NpPlan $spec $original $current $parent $children $admins $ScriptSha256
  $null=Get-NpService $spec $root;Assert-NpEvidence $spec $root $candidate.plan.target_sd_sha256 ($Mode -ceq 'rollback')
  if($Mode -ceq 'plan'){
   if($candidate.current_hash -cne $candidate.plan.original_sd_sha256){throw 'namespace_current_drift'}
   if($candidate.PSObject.Properties['closure']){Assert-NpClosureState $candidate @($spec.closure_original_sd_sha256)}
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
