// Isolated SCM acceptance probe. Only a freshly marked disposable VM is valid.
// Never used by the HA controller or against an existing production VM.
using System;
using System.IO;
using System.Text;
using System.Text.RegularExpressions;
using System.Collections.Generic;
using System.Security.Principal;
using System.Threading;
using System.Security.Cryptography;
#if !PORTABLE_FIXTURE
using System.Management;
using System.ServiceProcess;
using System.Diagnostics;
using System.Security.AccessControl;
#endif

namespace HaProviderProbeV1 {
 public sealed class Settings {
  public readonly Dictionary<string,string> Values;
  static readonly string[] Keys={"format","service_name","service_sid","installation_id","vm_id","operation","expected_hyperv_role","target_descriptor_b64","target_descriptor_sha256"};
  public Settings(string text) {
   if(text==null||text.Length>16384||text.IndexOf('\r')>=0||text.IndexOf('\0')>=0)throw new InvalidOperationException();
   string[] lines=text.Split('\n');if(lines.Length!=Keys.Length+1||lines[Keys.Length]!="")throw new InvalidOperationException();
   Values=new Dictionary<string,string>(StringComparer.Ordinal);
   for(int i=0;i<Keys.Length;i++){string p=Keys[i]+"=";if(!lines[i].StartsWith(p,StringComparison.Ordinal))throw new InvalidOperationException();Values.Add(Keys[i],lines[i].Substring(p.Length));}
   if(Values["format"]!="ha-provider-probe-v3"||!Regex.IsMatch(Values["service_name"],"\\AHaProbe_[0-9a-f]{16}\\z")||
      !Regex.IsMatch(Values["service_sid"],"\\AS-1-5-80-(?:[0-9]{1,10}-){4}[0-9]{1,10}\\z")||
      !Regex.IsMatch(Values["installation_id"],"\\A[0-9a-f]{64}\\z")||
      !Regex.IsMatch(Values["vm_id"],"\\A[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\\z")||
      (Values["operation"]!="power_on"&&Values["operation"]!="power_off"&&Values["operation"]!="settings")||
      (Values["expected_hyperv_role"]!="true"&&Values["expected_hyperv_role"]!="false"))throw new InvalidOperationException();
   if(Values["operation"]=="settings")Descriptor=new TargetDescriptor(Decode(Values["target_descriptor_b64"]),Values["target_descriptor_sha256"],Values["installation_id"],Values["vm_id"]);
   else if(Values["target_descriptor_b64"]!=""||Values["target_descriptor_sha256"]!="")throw new InvalidOperationException();
  }
  public readonly TargetDescriptor Descriptor;
  public static string Hash(byte[] bytes){using(var h=SHA256.Create())return BitConverter.ToString(h.ComputeHash(bytes)).Replace("-","").ToLowerInvariant();}
  public static string Decode(string value){byte[] b=Convert.FromBase64String(value);if(Convert.ToBase64String(b)!=value)throw new InvalidOperationException();return new UTF8Encoding(false,true).GetString(b);}
  public string Marker(){return "ha-owned-provider-probe:"+Values["installation_id"];}
  // Provider ReturnValue 32769 is access denied; HRESULT WBEM_E_ACCESS_DENIED
  // or E_ACCESSDENIED is usable only after a valid operation was attempted.
  public static bool AccessDenied(uint code){return code==32769||code==0x80041003||code==0x80070005;}
  public static bool AuthorizationDenial(bool attempted,uint code){return attempted&&AccessDenied(code);}
  public static bool NativeRelativeTarget(bool instance,string actualClass,string expectedClass,string server,string ns,string full,string relative,string source){
   // Windows maps an implicit local server to "." even for a relative path.
   // Reject explicit prefixes through both the namespace and full-path checks.
   return instance&&!String.IsNullOrEmpty(source)&&!String.IsNullOrEmpty(expectedClass)&&actualClass==expectedClass&&(String.IsNullOrEmpty(server)||server==".")&&
    String.IsNullOrEmpty(ns)&&full==relative&&relative==source;
  }
  // Hyper-V v2 uses VirtualSystemType, not the v1 SettingType property.
  // https://learn.microsoft.com/en-us/windows/win32/hyperv_v2/msvm-settingsdefinestate
  // https://learn.microsoft.com/en-us/windows/win32/hyperv_v2/msvm-virtualsystemsettingdata
  public static bool CurrentReadback(string type,string identifier,string expected,string actualPath,string expectedPath,string[] notes,string marker){
   return CurrentSetting(type,identifier,expected)&&String.Equals(actualPath,expectedPath,StringComparison.Ordinal)&&notes!=null&&notes.Length==1&&notes[0]==marker+":tested";
  }
  public static bool CurrentSetting(string type,string identifier,string expected){
   return type=="Microsoft:Hyper-V:System:Realized"&&identifier!=null&&expected!=null&&
    Regex.IsMatch(identifier,"\\A[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\\z")&&
    String.Equals(identifier,expected,StringComparison.OrdinalIgnoreCase);
  }
 }
 public sealed class TargetDescriptor {
  public readonly Dictionary<string,string> Values;
  static readonly string[] Keys={"format","installation_id","vm_id","settings_vm_state","settings_virtual_system_type","settings_virtual_system_identifier","settings_service_path_b64","settings_target_path_b64","settings_payload_sha256","settings_payload_bytes"};
  public TargetDescriptor(string text,string hash,string installation,string vm){
   if(text==null||text.Length>8192||text.IndexOf('\r')>=0||text.IndexOf('\0')>=0||!Regex.IsMatch(hash,"\\A[0-9a-f]{64}\\z")||Settings.Hash(Encoding.UTF8.GetBytes(text))!=hash)throw new InvalidOperationException();
   string[] lines=text.Split('\n');if(lines.Length!=Keys.Length+1||lines[Keys.Length]!="")throw new InvalidOperationException();
   Values=new Dictionary<string,string>(StringComparer.Ordinal);
   for(int i=0;i<Keys.Length;i++){string p=Keys[i]+"=";if(!lines[i].StartsWith(p,StringComparison.Ordinal))throw new InvalidOperationException();Values.Add(Keys[i],lines[i].Substring(p.Length));}
   int size;
   if(Values["format"]!="ha-provider-target-v3"||Values["installation_id"]!=installation||Values["vm_id"]!=vm||Values["settings_vm_state"]!="Running"||
      !Settings.CurrentSetting(Values["settings_virtual_system_type"],Values["settings_virtual_system_identifier"],vm)||
      !Regex.IsMatch(Values["settings_payload_sha256"],"\\A[0-9a-f]{64}\\z")||
      !Regex.IsMatch(Values["settings_payload_bytes"],"\\A[1-9][0-9]{0,6}\\z")||!Int32.TryParse(Values["settings_payload_bytes"],out size)||size>1048576)throw new InvalidOperationException();
   foreach(string k in new[]{"settings_service_path_b64","settings_target_path_b64"}){string path=Settings.Decode(Values[k]);if(path.Length<1||path.Length>2048||path.IndexOf('\n')>=0||path.IndexOf('\r')>=0||path.IndexOf('\0')>=0)throw new InvalidOperationException();}
  }
  public string VerifyPayload(byte[] bytes,string marker){
   if(bytes.Length!=Int32.Parse(Values["settings_payload_bytes"])||Settings.Hash(bytes)!=Values["settings_payload_sha256"])throw new InvalidOperationException();
   string text=new UTF8Encoding(false,true).GetString(bytes);
   // Parent serialized an actual, VM-bound typed object. These are additional
   // consistency checks, not an independent XML parser or provenance proof.
   if(text.IndexOf(marker+":tested",StringComparison.Ordinal)<0||text.IndexOf(Values["settings_virtual_system_identifier"],StringComparison.OrdinalIgnoreCase)<0)throw new InvalidOperationException();
   return text;
  }
 }
#if !PORTABLE_FIXTURE
 public sealed class Probe : ServiceBase {
  readonly Settings cfg; readonly string output; volatile bool stopped;
  public Probe(Settings value,string path){cfg=value;output=path;ServiceName=cfg.Values["service_name"];AutoLog=false;CanStop=true;ExitCode=1;}
  protected override void OnStart(string[] args){if(args.Length!=0||stopped)throw new InvalidOperationException();ThreadPool.QueueUserWorkItem(delegate{RunProbe();});}
  protected override void OnStop(){stopped=true;}
  static ManagementPath BoundPath(string text,string className){
   var path=new ManagementPath(text);
   if(!Settings.NativeRelativeTarget(path.IsInstance,path.ClassName,className,path.Server,path.NamespacePath,path.Path,path.RelativePath,text))throw new InvalidOperationException();
   return path;
  }
  byte[] ReadPayload(){
   string path=Path.Combine(AppDomain.CurrentDomain.BaseDirectory,"settings.xml");
   if((File.GetAttributes(path)&(FileAttributes.ReparsePoint|FileAttributes.Directory))!=0)throw new InvalidOperationException();
   var acl=File.GetAccessControl(path,AccessControlSections.Owner|AccessControlSections.Access);
   string owner=acl.GetOwner(typeof(SecurityIdentifier)).Value;
   if(owner!="S-1-5-18"&&owner!="S-1-5-32-544")throw new InvalidOperationException();
   bool reader=false;
   foreach(FileSystemAccessRule rule in acl.GetAccessRules(true,true,typeof(SecurityIdentifier))){
    string sid=rule.IdentityReference.Value;
    if(rule.AccessControlType!=AccessControlType.Allow)throw new InvalidOperationException();
    if(sid==cfg.Values["service_sid"]){if((rule.FileSystemRights&~(FileSystemRights.ReadAndExecute|FileSystemRights.Synchronize))!=0)throw new InvalidOperationException();reader=true;}
    else if(sid!="S-1-5-18"&&sid!="S-1-5-32-544")throw new InvalidOperationException();
   }
   if(!reader)throw new InvalidOperationException();
   using(var file=new FileStream(path,FileMode.Open,FileAccess.Read,FileShare.Read)){
    int size=Int32.Parse(cfg.Descriptor.Values["settings_payload_bytes"]);
    if(file.Length!=size)throw new InvalidOperationException();byte[] bytes=new byte[size];int offset=0;
    while(offset<size){int count=file.Read(bytes,offset,size-offset);if(count<=0)throw new InvalidOperationException();offset+=count;}
    return bytes;
   }
  }
  void RunProbe(){
   bool attempted=false,complete=false,authorized=false,admin=false,role=false;
   bool settingsSerialized=false,settingsReadback=false,pathsBound=false,methodResult=false,denied=false,currentAssociation=false;string currentPathHash="",currentInstanceHash="";uint code=0xffffffff;
   string stage="identity";
   try {
    using(var id=WindowsIdentity.GetCurrent()){
     var principal=new WindowsPrincipal(id);admin=principal.IsInRole(new SecurityIdentifier("S-1-5-32-544"));role=principal.IsInRole(new SecurityIdentifier("S-1-5-32-578"));
     if(id.User.Value!=cfg.Values["service_sid"]||Process.GetCurrentProcess().SessionId!=0||admin||role!=(cfg.Values["expected_hyperv_role"]=="true"))throw new InvalidOperationException();
    }
    stage="input_validation";
    string operation=cfg.Values["operation"],serialized=null;
    ManagementPath servicePath=null,settingsPath=null;
    var vmPath=BoundPath("Msvm_ComputerSystem.CreationClassName=\"Msvm_ComputerSystem\",Name=\""+cfg.Values["vm_id"]+"\"","Msvm_ComputerSystem");
    if(operation=="settings"){
     var descriptor=cfg.Descriptor;
     serialized=descriptor.VerifyPayload(ReadPayload(),cfg.Marker());settingsSerialized=true;
     servicePath=BoundPath(Settings.Decode(descriptor.Values["settings_service_path_b64"]),"Msvm_VirtualSystemManagementService");
     settingsPath=BoundPath(Settings.Decode(descriptor.Values["settings_target_path_b64"]),"Msvm_VirtualSystemSettingData");
    }
    pathsBound=true;
    stage="schema_query";
    var options=new ConnectionOptions();options.EnablePrivileges=false;options.Timeout=TimeSpan.FromSeconds(5);
    var scope=new ManagementScope(@"\\.\root\virtualization\v2",options);scope.Connect();
    string className=operation=="settings"?"Msvm_VirtualSystemManagementService":"Msvm_ComputerSystem";
    string method=operation=="settings"?"ModifySystemSettings":"RequestStateChange";
    // Class metadata only. Never instance.Get/GetRelated/GetMethodParameters
    // on the target before Invoke: a query denial is not method authorization.
    using(var schema=new ManagementClass(scope,new ManagementPath(className),null))
    using(var input=schema.GetMethodParameters(method))
    using(var target=new ManagementObject(scope,operation=="settings"?servicePath:vmPath,null)){
     if(operation=="settings")input["SystemSettings"]=serialized;
     else input["RequestedState"]=(ushort)(operation=="power_on"?2:3);
     var invoke=new InvokeMethodOptions();invoke.Timeout=TimeSpan.FromSeconds(5);
     ManagementBaseObject answer=null;
     if(stopped)throw new InvalidOperationException();
     stage="valid_operation";attempted=true;
     // Only an exception thrown by this actual ExecMethod call can establish
     // HRESULT denial. Job/readback query errors remain incomplete/unknown.
     try{answer=target.InvokeMethod(method,input,invoke);}
     catch(ManagementException error){code=unchecked((uint)error.ErrorCode);methodResult=true;denied=Settings.AuthorizationDenial(attempted,code);complete=denied;}
     catch(System.Runtime.InteropServices.COMException error){code=unchecked((uint)error.ErrorCode);methodResult=true;denied=Settings.AuthorizationDenial(attempted,code);complete=denied;}
     if(answer!=null)using(answer){
      code=Convert.ToUInt32(answer["ReturnValue"]);methodResult=true;
      denied=Settings.AuthorizationDenial(attempted,code);complete=denied;
      if(code==4096){
       string jobPath=answer["Job"] as string;if(String.IsNullOrEmpty(jobPath))throw new InvalidOperationException();
       using(var job=new ManagementObject(scope,new ManagementPath(jobPath),null)){
        var clock=Stopwatch.StartNew();
        while(!stopped&&clock.Elapsed.TotalSeconds<10){job.Get();uint stateJob=Convert.ToUInt32(job["JobState"]);if(stateJob==7||stateJob==8||stateJob==9||stateJob==10){uint jobCode=Convert.ToUInt32(job["ErrorCode"]);code=(stateJob==7&&jobCode==0)?0:0xffffffff;break;}Thread.Sleep(50);}
       }
      }
      authorized=code==0;complete=authorized||denied;
      if(operation=="settings"&&authorized&&cfg.Values["expected_hyperv_role"]=="true"){
       // A fresh object at an old path alone is insufficient after a power
       // transition. Resolve the current association again, only after Exec.
       using(var search=new ManagementObjectSearcher(scope,new ObjectQuery("SELECT * FROM Msvm_ComputerSystem WHERE Name='"+cfg.Values["vm_id"]+"'")))
       using(var found=search.Get()){
        if(found.Count!=1)throw new InvalidOperationException();int current=0;
        foreach(ManagementObject vm in found)using(vm){
         if(Convert.ToUInt32(vm["EnabledState"])!=2)throw new InvalidOperationException();
         using(var related=vm.GetRelated("Msvm_VirtualSystemSettingData","Msvm_SettingsDefineState",null,null,null,null,false,null)){
          foreach(ManagementObject readback in related)using(readback){
           if(Settings.CurrentSetting(readback["VirtualSystemType"] as string,readback["VirtualSystemIdentifier"] as string,cfg.Values["vm_id"])){
            current++;currentPathHash=Settings.Hash(Encoding.UTF8.GetBytes(readback.Path.RelativePath));currentInstanceHash=Settings.Hash(Encoding.UTF8.GetBytes(Convert.ToString(readback["InstanceID"])));currentAssociation=readback.Path.RelativePath==settingsPath.RelativePath;
            settingsReadback=Settings.CurrentReadback(readback["VirtualSystemType"] as string,readback["VirtualSystemIdentifier"] as string,cfg.Values["vm_id"],readback.Path.RelativePath,settingsPath.RelativePath,readback["Notes"] as string[],cfg.Marker());
           }
          }
         }
        }
        if(current!=1){settingsReadback=false;currentAssociation=false;}
       }
       if(!settingsReadback)complete=false;
      }
     }
    }
   }catch{complete=false;}
   if(stopped){complete=false;authorized=false;denied=false;}
   try{
    var process=Process.GetCurrentProcess();
    string json="{\"schema_version\":3,\"scope\":\"owned_vm_provider_role_probe\",\"process_id\":"+process.Id+",\"process_start_ticks\":\""+process.StartTime.ToUniversalTime().Ticks+"\",\"operation\":\""+cfg.Values["operation"]+"\",\"stage\":\""+stage+"\",\"attempted\":"+attempted.ToString().ToLowerInvariant()+",\"complete\":"+complete.ToString().ToLowerInvariant()+",\"authorized\":"+authorized.ToString().ToLowerInvariant()+",\"authorization_denied\":"+denied.ToString().ToLowerInvariant()+",\"return_code\":"+code+",\"administrator\":"+admin.ToString().ToLowerInvariant()+",\"hyperv_role\":"+role.ToString().ToLowerInvariant()+",\"settings_serialized_value_present\":"+settingsSerialized.ToString().ToLowerInvariant()+",\"settings_readback_verified\":"+settingsReadback.ToString().ToLowerInvariant()+",\"native_target_paths_bound\":"+pathsBound.ToString().ToLowerInvariant()+",\"method_result_verified\":"+methodResult.ToString().ToLowerInvariant()+",\"target_descriptor_sha256\":\""+cfg.Values["target_descriptor_sha256"]+"\",\"settings_payload_sha256\":\""+(cfg.Descriptor==null?"":cfg.Descriptor.Values["settings_payload_sha256"])+"\",\"settings_current_path_sha256\":\""+currentPathHash+"\",\"settings_current_instance_sha256\":\""+currentInstanceHash+"\",\"settings_current_association_verified\":"+currentAssociation.ToString().ToLowerInvariant()+"}\n";
    using(var file=new FileStream(output,FileMode.CreateNew,FileAccess.Write,FileShare.None)){byte[] bytes=Encoding.UTF8.GetBytes(json);file.Write(bytes,0,bytes.Length);file.Flush(true);}
    ExitCode=complete?0:1;
   }catch{ExitCode=1;}
   Stop();
  }
 }
 public static class Entry {
  public static void Main(string[] args){
   if(args.Length!=0||!Environment.Is64BitProcess)throw new InvalidOperationException();
   string root=AppDomain.CurrentDomain.BaseDirectory;
   var cfg=new Settings(File.ReadAllText(Path.Combine(root,"probe.args"),new UTF8Encoding(false,true)));
   ServiceBase.Run(new Probe(cfg,Path.Combine(root,"result","probe.json")));
  }
 }
#endif
}
