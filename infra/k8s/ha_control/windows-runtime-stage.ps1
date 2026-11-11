param([string]$Manifest,[string]$ManifestPath,[string]$ManifestSha256,
      [string]$OwnedProfile,[string]$OwnedProfileSha256,[switch]$Go)
# Stage code only into a new private directory. No SCM, policy, VM or credential installer.
$ErrorActionPreference='Stop'
$WarningPreference='SilentlyContinue'
$VerbosePreference='SilentlyContinue'
$InformationPreference='SilentlyContinue'
$DebugPreference='SilentlyContinue'
Set-StrictMode -Version Latest
$script:RuntimeOwnedSpec=$null
$script:RuntimeOwnedRoot=''
$script:RuntimeStageSource=@'
using System;
using System.IO;
using System.IO.Compression;
using System.Text;
using System.Text.RegularExpressions;
using System.Collections.Generic;
using System.Security.Cryptography;
using System.Runtime.InteropServices;
using System.Diagnostics;
using System.Threading.Tasks;
using Microsoft.Win32.SafeHandles;
namespace HaRuntimeStageV1 {
 public static class Json {
  sealed class Parser {
   readonly string text; int at; int nodes;
   public Parser(string s){text=s;}
   void White(){while(at<text.Length&&(text[at]==' '||text[at]=='\r'||text[at]=='\n'||text[at]=='\t'))at++;}
   void Need(bool ok){if(!ok)throw new InvalidOperationException("runtime_stage_json_rejected");}
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
    Need(depth<=8&&++nodes<=65536);White();Need(at<text.Length);char c=text[at];
    if(c=='"')return String();
    if(c=='{'){at++;White();var result=new Dictionary<string,object>(StringComparer.Ordinal);
     if(at<text.Length&&text[at]=='}'){at++;return result;}
     while(true){White();string key=String();Need(key.Length>0&&key.Length<=200&&!result.ContainsKey(key));White();Need(at<text.Length&&text[at++]==':');result.Add(key,Value(depth+1));White();Need(at<text.Length);char end=text[at++];if(end=='}')return result;Need(end==',');}
    }
    if(c=='['){at++;White();var values=new List<object>();if(at<text.Length&&text[at]==']'){at++;return values;}
     while(true){Need(values.Count<8192);values.Add(Value(depth+1));White();Need(at<text.Length);char end=text[at++];if(end==']')return values;Need(end==',');}}
    foreach(string word in new string[]{"true","false","null"})if(at+word.Length<=text.Length&&System.String.CompareOrdinal(text,at,word,0,word.Length)==0){at+=word.Length;return word=="null"?null:(object)(word=="true");}
    int start=at;if(c=='-')at++;Need(at<text.Length&&text[at]>='0'&&text[at]<='9');
    if(text[at]=='0')at++;else while(at<text.Length&&text[at]>='0'&&text[at]<='9')at++;
    long integer;Need(Int64.TryParse(text.Substring(start,at-start),System.Globalization.NumberStyles.AllowLeadingSign,System.Globalization.CultureInfo.InvariantCulture,out integer));return integer;
   }
   public object Parse(){object value=Value(0);White();Need(at==text.Length);return value;}
  }
  public static Dictionary<string,object> Object(string text,int maximum){
   if(text==null||text.Length>maximum||Encoding.UTF8.GetByteCount(text)>maximum)throw new InvalidOperationException("runtime_stage_json_size");
   object value=new Parser(text).Parse();var result=value as Dictionary<string,object>;if(result==null)throw new InvalidOperationException("runtime_stage_json_object_required");return result;
  }
 }
 public sealed class FileSpec {public string Filename,Hash,Component;public long Bytes;}
 public sealed class Spec {public string SourceCommit,InstallationId,ArchiveHash,ServiceName,ServiceSid;public long ArchiveBytes,TotalBytes;public bool HostStaged;public Dictionary<string,FileSpec> Files;public HashSet<string> Directories;}
 public static class Rules {
  public static void Need(bool value){if(!value)throw new InvalidOperationException("runtime_stage_input_rejected");}
  public static bool Match(string value,string pattern){return value!=null&&Regex.IsMatch(value,"\\A(?:"+pattern+")\\z",RegexOptions.CultureInvariant);}
  public static string Text(Dictionary<string,object> value,string key){object v;Need(value.TryGetValue(key,out v)&&v is string);return (string)v;}
  public static long Number(Dictionary<string,object> value,string key){object v;Need(value.TryGetValue(key,out v)&&v is long);return (long)v;}
  public static void Keys(Dictionary<string,object> value,string[] keys){Need(value.Count==keys.Length);foreach(string key in keys)Need(value.ContainsKey(key));}
  public static string Hash(byte[] bytes){using(var h=SHA256.Create())return Hex(h.ComputeHash(bytes));}
  public static string Hex(byte[] bytes){return BitConverter.ToString(bytes).Replace("-","").ToLowerInvariant();}
  public static string StreamHash(Stream stream){using(var h=SHA256.Create())return Hex(h.ComputeHash(stream));}
  public static string ServiceName(string installation){Need(Match(installation,"[0-9a-f]{64}"));return "N100HA_"+installation.Substring(0,24)+"_controller";}
  public static string ServiceSid(string name){Need(Match(name,"[A-Za-z0-9_-]{1,80}"));byte[] digest;using(var h=SHA1.Create())digest=h.ComputeHash(Encoding.Unicode.GetBytes(name.ToUpperInvariant()));var b=new StringBuilder("S-1-5-80");for(int i=0;i<5;i++){uint n=(uint)(digest[4*i]|digest[4*i+1]<<8|digest[4*i+2]<<16|digest[4*i+3]<<24);b.Append('-').Append(n.ToString(System.Globalization.CultureInfo.InvariantCulture));}return b.ToString();}
  public static string Name(string name){
   Need(name!=null&&name.Length<=180&&Match(name,"[A-Za-z0-9_.+/-]+")&&!name.StartsWith("/")&&!name.EndsWith("/")&&!name.EndsWith(".pth",StringComparison.OrdinalIgnoreCase));
   string[] parts=name.Split('/');Need(parts.Length<=16);
   foreach(string p in parts){Need(p.Length>0&&p!="."&&p!=".."&&!p.EndsWith(".")&&!p.EndsWith(" "));string stem=p.Split('.')[0].ToUpperInvariant();Need(stem!="CON"&&stem!="PRN"&&stem!="AUX"&&stem!="NUL"&&!Regex.IsMatch(stem,"\\A(?:COM|LPT)[1-9]\\z"));}
   return name;
  }
  public static Spec Manifest(string text){
   var v=Json.Object(text,2097152);Keys(v,new[]{"schema_version","source_commit","installation_id","archive_sha256","archive_bytes","service_name","service_sid","files"});Need(Number(v,"schema_version")==1);
   var s=new Spec();s.SourceCommit=Text(v,"source_commit");s.InstallationId=Text(v,"installation_id");s.ArchiveHash=Text(v,"archive_sha256");s.ArchiveBytes=Number(v,"archive_bytes");s.ServiceName=Text(v,"service_name");s.ServiceSid=Text(v,"service_sid");
   Need(Match(s.SourceCommit,"[0-9a-f]{40}")&&Match(s.InstallationId,"[0-9a-f]{64}")&&Match(s.ArchiveHash,"[0-9a-f]{64}")&&s.ArchiveBytes>=22&&s.ArchiveBytes<=1073741824&&s.ServiceName==ServiceName(s.InstallationId)&&s.ServiceSid==ServiceSid(s.ServiceName));
   var files=v["files"] as Dictionary<string,object>;Need(files!=null&&files.Count>=776&&files.Count<=8192);s.Files=new Dictionary<string,FileSpec>(StringComparer.Ordinal);var folded=new HashSet<string>(StringComparer.OrdinalIgnoreCase);int python=0,controller=0,host=0;
   foreach(var pair in files){string name=Name(pair.Key);Need(folded.Add(name));var f=pair.Value as Dictionary<string,object>;Need(f!=null);Keys(f,new[]{"filename","sha256","bytes","component"});var item=new FileSpec{Filename=Text(f,"filename"),Hash=Text(f,"sha256"),Bytes=Number(f,"bytes"),Component=Text(f,"component")};Need(item.Filename==name&&Match(item.Hash,"[0-9a-f]{64}")&&item.Bytes>=0&&item.Bytes<=67108864);
    if(item.Component=="python-runtime"){Need(name.StartsWith("python/",StringComparison.Ordinal));python++;}
    else if(item.Component=="controller-code"){Need(name.StartsWith("controller/",StringComparison.Ordinal));controller++;}
    else{Need(item.Component=="service-host"&&(name=="host/windows-service-host.exe"||name=="host/service.args"));host++;}
    s.TotalBytes+=item.Bytes;Need(s.TotalBytes<=536870912);s.Files.Add(name,item);
   }
   Need(python==776&&controller>0&&(host==0||host==2));s.HostStaged=host==2;
   foreach(string name in new[]{"python/python.exe","python/python313.dll","python/python313.zip","python/python313._pth","python/Lib/site-packages/psycopg/__init__.py","python/Lib/site-packages/psycopg_binary/__init__.py","python/Lib/site-packages/tzdata/__init__.py","controller/source-manifest.json","controller/files/ha_control/launcher.py"})Need(s.Files.ContainsKey(name));
   s.Directories=new HashSet<string>(StringComparer.Ordinal);var directoryCase=new Dictionary<string,string>(StringComparer.OrdinalIgnoreCase);foreach(string name in s.Files.Keys){string prefix=name;while(prefix.LastIndexOf('/')>=0){prefix=prefix.Substring(0,prefix.LastIndexOf('/'));Need(!folded.Contains(prefix));string existing;if(directoryCase.TryGetValue(prefix,out existing))Need(existing==prefix);else directoryCase.Add(prefix,prefix);s.Directories.Add(prefix);Need(s.Directories.Count<=8192);}}
   return s;
  }
  public static void Zip(ZipArchive archive,Spec spec){
   Need(archive.Entries.Count==spec.Files.Count&&archive.Entries.Count<=8192);var seen=new HashSet<string>(StringComparer.OrdinalIgnoreCase);long total=0;
   foreach(var e in archive.Entries){string name=Name(e.FullName);Need(seen.Add(name)&&spec.Files.ContainsKey(name));int kind=(e.ExternalAttributes>>16)&0xf000;Need((kind==0||kind==0x8000)&&(e.ExternalAttributes&0x410)==0);Need(e.Length==spec.Files[name].Bytes&&e.Length<=67108864);using(var check=e.Open())Need(check.CanRead);total+=e.Length;Need(total<=536870912);}
   Need(total==spec.TotalBytes);
  }
 }
 public static class Engine {
  public static string Destination(string root,string name){Rules.Name(name);string full=Path.GetFullPath(Path.Combine(root,name.Replace('/',Path.DirectorySeparatorChar)));string prefix=Path.GetFullPath(root).TrimEnd(Path.DirectorySeparatorChar)+Path.DirectorySeparatorChar;Rules.Need(full.StartsWith(prefix,StringComparison.OrdinalIgnoreCase)&&full.Length<=240);return full;}
  static void NoReparse(string path){Rules.Need((File.GetAttributes(path)&FileAttributes.ReparsePoint)==0);}
  public static void Extract(ZipArchiveEntry entry,FileSpec spec,string root){
   Rules.Need(entry.FullName==spec.Filename);string target=Destination(root,spec.Filename);string parent=Path.GetDirectoryName(target);string current=Path.GetFullPath(root);NoReparse(current);string relative=Path.GetDirectoryName(spec.Filename.Replace('/',Path.DirectorySeparatorChar));if(!String.IsNullOrEmpty(relative))foreach(string part in relative.Split(Path.DirectorySeparatorChar)){current=Path.Combine(current,part);if(Directory.Exists(current)||File.Exists(current))NoReparse(current);else Directory.CreateDirectory(current);NoReparse(current);}
   for(string p=parent;;p=Path.GetDirectoryName(p)){NoReparse(p);if(String.Equals(p,Path.GetFullPath(root),StringComparison.OrdinalIgnoreCase))break;Rules.Need(p!=null&&p.Length>Path.GetPathRoot(p).Length);}
   using(var source=entry.Open())using(var output=new FileStream(target,FileMode.CreateNew,FileAccess.Write,FileShare.None))using(var hash=SHA256.Create()){
    var buffer=new byte[1048576];long count=0;int read;
    while((read=source.Read(buffer,0,buffer.Length))>0){Rules.Need(read<=spec.Bytes-count);hash.TransformBlock(buffer,0,read,buffer,0);output.Write(buffer,0,read);count+=read;}
    hash.TransformFinalBlock(new byte[0],0,0);output.Flush(true);Rules.Need(count==spec.Bytes&&Rules.Hex(hash.Hash)==spec.Hash);
   }
  }
  public static string[] Inventory(string root,Spec spec,bool readOnly){
   var files=new List<string>();var seen=new HashSet<string>(StringComparer.OrdinalIgnoreCase);var pending=new Stack<string>();pending.Push(root);
   while(pending.Count>0){string dir=pending.Pop();NoReparse(dir);foreach(string p in Directory.GetFileSystemEntries(dir)){NoReparse(p);if(Directory.Exists(p)){string relative=p.Substring(root.TrimEnd(Path.DirectorySeparatorChar).Length+1).Replace(Path.DirectorySeparatorChar,'/');Rules.Need(spec.Directories.Contains(relative));pending.Push(p);continue;}string name=p.Substring(root.TrimEnd(Path.DirectorySeparatorChar).Length+1).Replace(Path.DirectorySeparatorChar,'/');Rules.Need(seen.Add(name)&&spec.Files.ContainsKey(name));var info=new FileInfo(p);Rules.Need(info.Length==spec.Files[name].Bytes&&(!readOnly||(info.Attributes&FileAttributes.ReadOnly)!=0));using(var f=new FileStream(p,FileMode.Open,FileAccess.Read,FileShare.Read))Rules.Need(Rules.StreamHash(f)==spec.Files[name].Hash);files.Add(p);}}
   Rules.Need(files.Count==spec.Files.Count);return files.ToArray();
  }
  public static void RuntimePathFile(string root){string path=Path.Combine(root,"python","python313._pth");Rules.Need(File.ReadAllText(path,new UTF8Encoding(false,true))=="python313.zip\n.\nLib/site-packages\n");}
 }
 public static class Native {
  [StructLayout(LayoutKind.Sequential)] struct Info {public uint Attr;public System.Runtime.InteropServices.ComTypes.FILETIME Created,Accessed,Written;public uint Volume,High,Low,Links,IndexHigh,IndexLow;}
  [StructLayout(LayoutKind.Sequential)] struct Unicode {public ushort Length,MaximumLength;public IntPtr Buffer;}
  [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern SafeFileHandle CreateFile(string p,uint a,uint s,IntPtr q,uint d,uint f,IntPtr t);
  [DllImport("kernel32.dll",SetLastError=true)] static extern bool GetFileInformationByHandle(SafeFileHandle h,out Info i);
  [DllImport("ntdll.dll")] static extern int RtlCreateServiceSid(ref Unicode name,byte[] sid,ref uint size);
  public static string Identity(string path){using(var h=CreateFile(path,0,1,IntPtr.Zero,3,0x00200000,IntPtr.Zero)){Info i;if(h.IsInvalid||!GetFileInformationByHandle(h,out i)||(i.Attr&0x400)!=0||i.Links!=1)throw new InvalidOperationException("runtime_stage_file_rejected");return i.Volume.ToString("x8")+":"+i.IndexHigh.ToString("x8")+i.IndexLow.ToString("x8");}}
  public static bool ServiceIdentity(string name,string expected){IntPtr text=Marshal.StringToHGlobalUni(name);try{var u=new Unicode{Length=(ushort)(name.Length*2),MaximumLength=(ushort)((name.Length+1)*2),Buffer=text};uint size=256;var bytes=new byte[size];if(RtlCreateServiceSid(ref u,bytes,ref size)!=0||size>bytes.Length)return false;return new System.Security.Principal.SecurityIdentifier(bytes,0).Value==expected;}finally{Marshal.FreeHGlobal(text);}}
 }
 public static class Smoke {
  const string Code="import sys,os,struct,_ssl,ctypes,psycopg,tzdata; assert sys.flags.isolated and sys.dont_write_bytecode and sys.version_info[:3]==(3,13,17) and struct.calcsize('P')==8 and psycopg.__version__=='3.3.8' and tzdata.__version__=='2026.2' and 'site' not in sys.modules and not (os.stat(sys.executable).st_mode&0o222) and not (os.stat(os.path.join(os.path.dirname(os.path.dirname(sys.executable)),'controller','source-manifest.json')).st_mode&0o222); print('RUNTIME_IMPORTS_PASS')";
  static string Small(StreamReader r,int maximum){var b=new StringBuilder();int c;while((c=r.Read())>=0){if(b.Length>=maximum)throw new InvalidOperationException();b.Append((char)c);}return b.ToString();}
  public static void Run(string python,string system){
   using(var p=new Process()){p.StartInfo=new ProcessStartInfo{FileName=python,Arguments="-I -B -c \""+Code+"\"",UseShellExecute=false,CreateNoWindow=true,RedirectStandardOutput=true,RedirectStandardError=true,WorkingDirectory=Path.GetDirectoryName(python)};p.StartInfo.EnvironmentVariables.Clear();p.StartInfo.EnvironmentVariables["SystemRoot"]=system;p.StartInfo.EnvironmentVariables["PATH"]=Path.Combine(system,"System32");bool started=false;var watch=Stopwatch.StartNew();
    try{Rules.Need(p.Start());started=true;IntPtr retained=p.Handle;var output=Task.Factory.StartNew(()=>Small(p.StandardOutput,256));var error=Task.Factory.StartNew(()=>Small(p.StandardError,1024));int remaining=Math.Max(0,15000-(int)watch.ElapsedMilliseconds);Rules.Need(p.WaitForExit(remaining));remaining=Math.Max(0,15000-(int)watch.ElapsedMilliseconds);Rules.Need(output.Wait(remaining));remaining=Math.Max(0,15000-(int)watch.ElapsedMilliseconds);Rules.Need(error.Wait(remaining));Rules.Need(p.ExitCode==0&&output.Result.Trim()=="RUNTIME_IMPORTS_PASS"&&error.Result.Length==0&&watch.ElapsedMilliseconds<=15000);}
    finally{if(started){try{if(!p.HasExited){p.Kill();p.WaitForExit(1000);}}finally{try{p.StandardOutput.Close();}catch{}try{p.StandardError.Close();}catch{}}}}
   }
  }
 }
}
'@
function Initialize-RuntimeStage {
    if('HaRuntimeStageV1.Rules' -as [type]){throw 'runtime_stage_type_collision'}
    Add-Type -AssemblyName System.IO.Compression -ErrorAction Stop
    if($PSVersionTable.PSEdition -ceq 'Desktop'){
        Add-Type -TypeDefinition $script:RuntimeStageSource -ReferencedAssemblies @('System.dll','System.Core.dll','System.IO.Compression.dll','System.IO.Compression.FileSystem.dll') -ErrorAction Stop
    }else{Add-Type -TypeDefinition $script:RuntimeStageSource -ErrorAction Stop}
}
function Get-RuntimeStageCaller {
    $identity=[Security.Principal.WindowsIdentity]::GetCurrent()
    try{return $identity.User.Value}finally{$identity.Dispose()}
}
function Get-RuntimeStageParent($Entry){if($Entry -is [IO.DirectoryInfo]){return $Entry.Parent};if($Entry -is [IO.FileInfo]){return $Entry.Directory};throw 'runtime_stage_entry_type_rejected'}
function Import-RuntimeOwnedSupport {
    $bytes=[IO.File]::ReadAllBytes((Join-Path $PSScriptRoot 'guest-owned-profile.ps1'))
    if([HaRuntimeStageV1.Rules]::Hash($bytes) -cne '437d02933c2800cea4097a79ee14db3052e8c522c15f132a52c7c094d6f84045'){throw 'runtime_stage_owned_support_changed'}
    return [scriptblock]::Create([Text.UTF8Encoding]::new($false,$true).GetString($bytes))
}
function Read-RuntimeOwnedProfile([string]$Encoded,[string]$Hash,[string]$ManifestHash,[string]$ScriptHash){
    if($Hash -cnotmatch '^[0-9a-f]{64}$' -or $Encoded.Length -gt 8192){throw 'runtime_stage_owned_profile_pin_required'}
    $raw=[Convert]::FromBase64String($Encoded)
    if([HaRuntimeStageV1.Rules]::Hash($raw) -cne $Hash){throw 'runtime_stage_owned_profile_changed'}
    $value=[HaRuntimeStageV1.Json]::Object(([Text.UTF8Encoding]::new($false,$true)).GetString($raw),6144)
    [HaRuntimeStageV1.Rules]::Keys($value,(@('installation_id','runtime_manifest_sha256','guest_stage_manifest_sha256')+@(Get-GuestOwnedFields)))
    if([HaRuntimeStageV1.Rules]::Text($value,'runtime_manifest_sha256') -cne $ManifestHash -or [HaRuntimeStageV1.Rules]::Text($value,'guest_stage_manifest_sha256') -cnotmatch '^[0-9a-f]{64}$'){throw 'runtime_stage_owned_manifest_changed'}
    $profile=[pscustomobject]$value
    Assert-GuestOwnedDeclaration $profile $ScriptHash
    return $profile
}
function Assert-RuntimeStageParentManifest($Stage,$Spec,$Owned){
    $keys=@('schema_version','source_commit','installation_id','compressed_sha256','compressed_bytes','vhdx_sha256','vhdx_bytes')
    if($null -ne $Owned){$keys+=@(Get-GuestOwnedFields)}
    [HaRuntimeStageV1.Rules]::Keys($Stage,$keys)
    $version=if($null -ne $Owned){2}else{1}
    if([HaRuntimeStageV1.Rules]::Number($Stage,'schema_version') -ne $version -or [HaRuntimeStageV1.Rules]::Text($Stage,'installation_id') -cne $Spec.InstallationId){throw 'runtime_stage_parent_installation_changed'}
    if($null -eq $Owned){return}
    if($Spec.HostStaged -or $Owned.installation_id -cne $Spec.InstallationId -or $Owned.executor_source_commit -cne $Spec.SourceCommit){throw 'runtime_stage_owned_source_or_host_changed'}
    foreach($key in @('root_profile','executor_source_commit','reservation_receipt_sha256','reservation_root_identity')){
        if([HaRuntimeStageV1.Rules]::Text($Stage,$key) -cne $Owned.$key){throw 'runtime_stage_owned_parent_changed'}
    }
    # The guest-stage executor differs from this script. Its original media
    # source and script hash remain bound by the full guest-stage manifest pin.
    Assert-GuestOwnedDeclaration ([pscustomobject]$Stage) ''
    if([HaRuntimeStageV1.Rules]::Text($Stage,'source_commit') -cnotmatch '^[0-9a-f]{40}$'){throw 'runtime_stage_guest_origin_rejected'}
}
function Assert-RuntimeStagePath([string]$Path,[string]$PrivateRoot,[string]$ReaderSid='',[bool]$InputFile=$false){
    if($Path -cnotmatch '^[A-Za-z]:\\' -or $Path.Substring(2).Contains(':') -or $Path.Contains('/') -or $Path -match '(^|\\)\.\.?($|\\)' -or $Path -match '[ .](\\|$)'){throw 'runtime_stage_path_rejected'}
    $allowed=@('S-1-5-18','S-1-5-32-544');if($InputFile){$allowed+=@(Get-RuntimeStageCaller)}
    $entry=Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    $osRoot=[IO.Path]::GetPathRoot([Environment]::GetFolderPath([Environment+SpecialFolder]::Windows))
    while($null -ne $entry){
        if($entry.Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'runtime_stage_reparse_rejected'}
        if($null -ne $script:RuntimeOwnedSpec -and $entry.FullName -ieq $script:RuntimeOwnedRoot){
            Assert-GuestOwnedRootAcl $entry.FullName $script:RuntimeOwnedSpec
            $entry=Get-RuntimeStageParent $entry
            continue
        }
        $private=($entry.FullName -ieq $PrivateRoot -or $entry.FullName.StartsWith($PrivateRoot+'\',[StringComparison]::OrdinalIgnoreCase))
        $sd=[Security.AccessControl.RawSecurityDescriptor]::new((Get-Acl -LiteralPath $entry.FullName -ErrorAction Stop).Sddl)
        $trustedRoot=($entry.FullName -ieq $osRoot -and $entry.FullName -cmatch '^[A-Za-z]:\\$' -and $sd.Owner.Value -ceq 'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464')
        if(($sd.Owner.Value -notin $allowed -and -not $trustedRoot) -or $null -eq $sd.DiscretionaryAcl -or $sd.DiscretionaryAcl.Count -eq 0){throw 'runtime_stage_owner_rejected'}
        foreach($ace in $sd.DiscretionaryAcl){
            if($ace -isnot [Security.AccessControl.CommonAce] -or $ace.IsCallback){throw 'runtime_stage_acl_rejected'}
            if(-not $private -and (([int]$ace.AceFlags) -band ([int][Security.AccessControl.AceFlags]::InheritOnly))){continue}
            if($ace.AceQualifier -eq [Security.AccessControl.AceQualifier]::AccessAllowed){
                $mask=[long]$ace.AccessMask -band 4294967295
                if($private){
                    if($ace.SecurityIdentifier.Value -notin $allowed -and ($ace.SecurityIdentifier.Value -cne $ReaderSid -or ($mask -band (4294967295 -bxor 1179817)) -ne 0)){throw 'runtime_stage_private_acl_rejected'}
                }elseif(($mask -band 269287488) -ne 0 -and $ace.SecurityIdentifier.Value -notin $allowed){throw 'runtime_stage_ancestor_writer_rejected'}
            }
        }
        $entry=Get-RuntimeStageParent $entry
    }
}
function Set-RuntimeStageAcl([string]$Path,[string]$Sid,[bool]$Directory){
    $acl=if($Directory){[Security.AccessControl.DirectorySecurity]::new()}else{[Security.AccessControl.FileSecurity]::new()}
    $acl.SetAccessRuleProtection($true,$false);$acl.SetOwner([Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))
    foreach($identity in @('S-1-5-18','S-1-5-32-544',$Sid)){
        $rights=if($identity -ceq $Sid){'ReadAndExecute'}else{'FullControl'}
        if($Directory){$rule=[Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new($identity),$rights,'ContainerInherit,ObjectInherit','None','Allow')}
        else{$rule=[Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new($identity),$rights,'Allow')}
        $acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $Path -AclObject $acl -ErrorAction Stop
}
function New-RuntimeStageDirectory([string]$Path,[string]$Sid){
    if(Test-Path -LiteralPath $Path){throw 'runtime_stage_existing_path'}
    $acl=[Security.AccessControl.DirectorySecurity]::new();$acl.SetAccessRuleProtection($true,$false);$acl.SetOwner([Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))
    foreach($identity in @('S-1-5-18','S-1-5-32-544',$Sid)){$rights=if($identity -ceq $Sid){'ReadAndExecute'}else{'FullControl'};$acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new($identity),$rights,'ContainerInherit,ObjectInherit','None','Allow'))}
    $null=[IO.Directory]::CreateDirectory($Path,$acl)
}
function Write-RuntimeStageEvent([string]$Path,[string]$Event,$Data,[bool]$New=$false){
    $bytes=[Text.UTF8Encoding]::new($false).GetBytes((([ordered]@{event=$Event;data=$Data})|ConvertTo-Json -Depth 6 -Compress)+"`n")
    $mode=if($New){[IO.FileMode]::CreateNew}else{[IO.FileMode]::Append}
    $file=[IO.File]::Open($Path,$mode,[IO.FileAccess]::Write,[IO.FileShare]::Read)
    try{$file.Write($bytes,0,$bytes.Length);$file.Flush($true)}finally{$file.Dispose()}
}
function Read-RuntimeStageInput([string]$Path,[string]$Root,[long]$Maximum){
    Assert-RuntimeStagePath $Path $Root '' $true
    $identity=[HaRuntimeStageV1.Native]::Identity($Path)
    $file=[IO.File]::Open($Path,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
    try{if($file.Length -gt $Maximum -or ([HaRuntimeStageV1.Native]::Identity($Path)) -cne $identity){throw 'runtime_stage_input_changed'};return $file}catch{$file.Dispose();throw}
}
function Assert-RuntimeStageSignatures([string]$Root){
    foreach($name in @('python.exe','python313.dll')){
        $signature=Get-AuthenticodeSignature -LiteralPath (Join-Path (Join-Path $Root 'python') $name) -ErrorAction Stop
        if($signature.Status -ne 'Valid' -or $null -eq $signature.SignerCertificate -or $signature.SignerCertificate.Subject -notmatch '(?:^|,\s*)(?:O|CN)=Python Software Foundation(?:,|$)'){throw 'runtime_stage_python_signature_rejected'}
    }
}
function Assert-RuntimeStageSealed([string]$Root,[string]$PrivateRoot,$Spec){
    $files=[HaRuntimeStageV1.Engine]::Inventory($Root,$Spec,$true)
    foreach($path in @($Root)+@(Get-ChildItem -LiteralPath $Root -Force -Recurse -ErrorAction Stop|ForEach-Object{$_.FullName})){
        Assert-RuntimeStagePath $path $PrivateRoot $Spec.ServiceSid
        $entry=Get-Item -LiteralPath $path -Force -ErrorAction Stop
        if(-not $entry.PSIsContainer){$null=[HaRuntimeStageV1.Native]::Identity($path)}
    }
    return $files.Count
}
function Invoke-RuntimeStage {
    if(-not $Go -or [Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT -or -not [Environment]::Is64BitProcess -or $PSVersionTable.PSEdition -cne 'Desktop'){throw 'runtime_stage_native_intent_required'}
    $identity=[Security.Principal.WindowsIdentity]::GetCurrent()
    try{if(-not ([Security.Principal.WindowsPrincipal]::new($identity)).IsInRole([Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))){throw 'runtime_stage_admin_required'}}finally{$identity.Dispose()}
    if($ManifestSha256 -cnotmatch '^[0-9a-f]{64}$' -or ([bool]$Manifest -eq [bool]$ManifestPath)){throw 'runtime_stage_manifest_pin_required'}
    Initialize-RuntimeStage
    $manifestFile=$null;$stageFile=$null;$zipFile=$null;$archive=$null;$ownedLease=$null;$owned=$null
    try{
        $parent=[Environment]::GetFolderPath([Environment+SpecialFolder]::CommonApplicationData)
        if([bool]$OwnedProfile -ne [bool]$OwnedProfileSha256){throw 'runtime_stage_owned_profile_pin_required'}
        if($OwnedProfile){
            . (Import-RuntimeOwnedSupport)
            $scriptHash=[HaRuntimeStageV1.Rules]::Hash([IO.File]::ReadAllBytes($PSCommandPath))
            $owned=Read-RuntimeOwnedProfile $OwnedProfile $OwnedProfileSha256 $ManifestSha256 $scriptHash
            $script:RuntimeOwnedRoot=Join-Path $parent ('N100HA_'+$owned.installation_id.Substring(0,24))
            $ownedLease=Open-GuestOwnedRoot $script:RuntimeOwnedRoot $owned $scriptHash
            $script:RuntimeOwnedSpec=$owned
        }
        if($ManifestPath){
            $prefix=[IO.Path]::GetDirectoryName($ManifestPath)
            if([IO.Path]::GetFileName($ManifestPath) -cne 'runtime-manifest.json' -or [IO.Path]::GetDirectoryName($prefix) -ine $parent -or [IO.Path]::GetFileName($prefix) -cnotmatch '^N100HA_[0-9a-f]{24}$'){throw 'runtime_stage_manifest_path_rejected'}
            if($null -ne $owned -and $prefix -ine $script:RuntimeOwnedRoot){throw 'runtime_stage_owned_root_changed'}
            $manifestFile=Read-RuntimeStageInput $ManifestPath $prefix 2097152
            $raw=New-Object byte[] ([int]$manifestFile.Length);$offset=0
            while($offset -lt $raw.Length){$read=$manifestFile.Read($raw,$offset,$raw.Length-$offset);if($read -le 0){throw 'runtime_stage_manifest_read_failed'};$offset+=$read}
        }else{if($Manifest.Length -gt 2796204){throw 'runtime_stage_manifest_limit'};$raw=[Convert]::FromBase64String($Manifest);if($raw.Length -gt 2097152){throw 'runtime_stage_manifest_limit'}}
        if(([HaRuntimeStageV1.Rules]::Hash($raw)) -cne $ManifestSha256){throw 'runtime_stage_manifest_changed'}
        $spec=[HaRuntimeStageV1.Rules]::Manifest(([Text.UTF8Encoding]::new($false,$true)).GetString($raw))
        if($null -ne $owned -and ($spec.InstallationId -cne $owned.installation_id -or $spec.SourceCommit -cne $owned.executor_source_commit -or $spec.HostStaged)){throw 'runtime_stage_owned_source_or_host_changed'}
        if(-not [HaRuntimeStageV1.Native]::ServiceIdentity($spec.ServiceName,$spec.ServiceSid)){throw 'runtime_stage_native_sid_mismatch'}
        $root=Join-Path $parent ('N100HA_'+$spec.InstallationId.Substring(0,24));Assert-RuntimeStagePath $root $root
        if($ManifestPath -and $prefix -ine $root){throw 'runtime_stage_installation_path_mismatch'}
        # Bind the full installation identity, not just its directory prefix.
        $stageFile=Read-RuntimeStageInput (Join-Path $root 'stage-manifest.json') $root 32768
        if($null -ne $owned){
            if([HaRuntimeStageV1.Rules]::StreamHash($stageFile) -cne $owned.guest_stage_manifest_sha256){throw 'runtime_stage_guest_stage_pin_changed'}
            $stageFile.Position=0
        }
        $reader=[IO.StreamReader]::new($stageFile,[Text.UTF8Encoding]::new($false,$true),$false,1024,$true)
        try{$stage=[HaRuntimeStageV1.Json]::Object($reader.ReadToEnd(),32768)}finally{$reader.Dispose()}
        Assert-RuntimeStageParentManifest $stage $spec $owned
        $zipPath=Join-Path $root 'runtime.zip';$zipFile=Read-RuntimeStageInput $zipPath $root 1073741824
        if($zipFile.Length -ne $spec.ArchiveBytes -or ([HaRuntimeStageV1.Rules]::StreamHash($zipFile)) -cne $spec.ArchiveHash){throw 'runtime_stage_archive_changed'};$zipFile.Position=0
        $archive=[IO.Compression.ZipArchive]::new($zipFile,[IO.Compression.ZipArchiveMode]::Read,$true)
        [HaRuntimeStageV1.Rules]::Zip($archive,$spec)
        $id='runtime-'+$ManifestSha256.Substring(0,24);$target=Join-Path $root $id;$journal=Join-Path $root ($id+'-stage-journal.jsonl')
        if((Test-Path -LiteralPath $target) -or (Test-Path -LiteralPath $journal)){throw 'runtime_stage_existing_partial_manual_reconcile'}
        $drive=[IO.DriveInfo]::new([IO.Path]::GetPathRoot($root));if($drive.AvailableFreeSpace -lt ($spec.TotalBytes*2+1073741824)){throw 'runtime_stage_disk_reserve_failed'}
        foreach($entry in $archive.Entries){$null=[HaRuntimeStageV1.Engine]::Destination($target,$entry.FullName)}
        $intent=@{manifest_sha256=$ManifestSha256;archive_sha256=$spec.ArchiveHash;source_commit=$spec.SourceCommit;installation_id=$spec.InstallationId;runtime_id=$id}
        if($null -ne $owned){$ownedLease.AssertCurrent();$intent.owned_profile_sha256=$OwnedProfileSha256;$intent.guest_stage_manifest_sha256=$owned.guest_stage_manifest_sha256}
        Write-RuntimeStageEvent $journal 'intent' $intent $true
        New-RuntimeStageDirectory $target $spec.ServiceSid
        foreach($entry in $archive.Entries){Write-RuntimeStageEvent $journal 'extract' @{filename=$entry.FullName};[HaRuntimeStageV1.Engine]::Extract($entry,$spec.Files[$entry.FullName],$target)}
        $null=[HaRuntimeStageV1.Engine]::Inventory($target,$spec,$false)
        [HaRuntimeStageV1.Engine]::RuntimePathFile($target)
        Write-RuntimeStageEvent $journal 'seal' @{}
        foreach($entry in @(Get-ChildItem -LiteralPath $target -Force -Recurse -ErrorAction Stop)){
            $null=if(-not $entry.PSIsContainer){[HaRuntimeStageV1.Native]::Identity($entry.FullName)}
            Set-RuntimeStageAcl $entry.FullName $spec.ServiceSid $entry.PSIsContainer
            if(-not $entry.PSIsContainer){[IO.File]::SetAttributes($entry.FullName,($entry.Attributes -bor [IO.FileAttributes]::ReadOnly))}
        }
        Set-RuntimeStageAcl $target $spec.ServiceSid $true
        $count=Assert-RuntimeStageSealed $target $root $spec
        Write-RuntimeStageEvent $journal 'authenticode' @{}
        Assert-RuntimeStageSignatures $target
        Write-RuntimeStageEvent $journal 'fresh_python_imports' @{}
        [HaRuntimeStageV1.Smoke]::Run((Join-Path (Join-Path $target 'python') 'python.exe'),([Environment]::GetFolderPath([Environment+SpecialFolder]::Windows)))
        $count=Assert-RuntimeStageSealed $target $root $spec
        $receipt=[ordered]@{scope='private_code_stage_only';schema_version=1;native_stage=$true;inventory_version=1;source_commit=$spec.SourceCommit;installation_id=$spec.InstallationId;manifest_sha256=$ManifestSha256;archive_sha256=$spec.ArchiveHash;runtime_id=$id;file_count=$count;readonly=$true;native_imports_verified=$true;python_version='3.13.17';psycopg_version='3.3.8';tzdata_version='2026.2';host_staged=$spec.HostStaged;SCM_registered=$false;group_changed=$false;namespace_changed=$false;VM_changed=$false;authority_issued=$false}
        if($null -ne $owned){$ownedLease.AssertCurrent();$receipt.schema_version=2;$receipt.owned_profile_sha256=$OwnedProfileSha256;$receipt.guest_stage_manifest_sha256=$owned.guest_stage_manifest_sha256;$receipt.reservation_receipt_sha256=$owned.reservation_receipt_sha256}
        Write-RuntimeStageEvent $journal 'sealed_code_stage' @{file_count=$count;native_code_only=$true;SCM_registered=$false;authority_issued=$false}
        $receipt|ConvertTo-Json -Compress
    }finally{
        if($null -ne $archive){$archive.Dispose()};if($null -ne $zipFile){$zipFile.Dispose()};if($null -ne $stageFile){$stageFile.Dispose()};if($null -ne $manifestFile){$manifestFile.Dispose()}
        if($null -ne $ownedLease){$ownedLease.Dispose()};$script:RuntimeOwnedSpec=$null;$script:RuntimeOwnedRoot=''
    }
}
if($MyInvocation.InvocationName -ne '.'){
    try{Invoke-RuntimeStage}catch{[ordered]@{scope='private_code_stage_only';status='BLOCKED';reason='runtime_stage_rejected_preserve_partial';SCM_registered=$false;authority_issued=$false}|ConvertTo-Json -Compress;exit 1}
}
