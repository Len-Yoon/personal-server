// Isolated SCM acceptance probe. Only a freshly marked disposable VM is valid.
// Never used by the HA controller or against an existing production VM.
using System;
using System.IO;
using System.Text;
using System.Text.RegularExpressions;
using System.Collections.Generic;
using System.Security.Principal;
using System.Threading;
#if !PORTABLE_FIXTURE
using System.Management;
using System.ServiceProcess;
using System.Diagnostics;
#endif

namespace HaProviderProbeV1 {
 public sealed class Settings {
  public readonly Dictionary<string,string> Values;
  static readonly string[] Keys={"format","service_name","service_sid","installation_id","vm_id","operation","expected_hyperv_role"};
  public Settings(string text) {
   if(text==null||text.Length>4096||text.IndexOf('\r')>=0||text.IndexOf('\0')>=0)throw new InvalidOperationException();
   string[] lines=text.Split('\n');if(lines.Length!=Keys.Length+1||lines[Keys.Length]!="")throw new InvalidOperationException();
   Values=new Dictionary<string,string>(StringComparer.Ordinal);
   for(int i=0;i<Keys.Length;i++){string p=Keys[i]+"=";if(!lines[i].StartsWith(p,StringComparison.Ordinal))throw new InvalidOperationException();Values.Add(Keys[i],lines[i].Substring(p.Length));}
   if(Values["format"]!="ha-provider-probe-v1"||!Regex.IsMatch(Values["service_name"],"\\AHaProbe_[0-9a-f]{16}\\z")||
      !Regex.IsMatch(Values["service_sid"],"\\AS-1-5-80-(?:[0-9]{1,10}-){4}[0-9]{1,10}\\z")||
      !Regex.IsMatch(Values["installation_id"],"\\A[0-9a-f]{64}\\z")||
      !Regex.IsMatch(Values["vm_id"],"\\A[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\\z")||
      (Values["operation"]!="power_on"&&Values["operation"]!="power_off"&&Values["operation"]!="settings")||
      (Values["expected_hyperv_role"]!="true"&&Values["expected_hyperv_role"]!="false"))throw new InvalidOperationException();
  }
  public string Marker(){return "ha-owned-provider-probe:"+Values["installation_id"];}
  // Provider ReturnValue 32769 is access denied; HRESULT WBEM_E_ACCESS_DENIED
  // or E_ACCESSDENIED is usable only after a valid operation was attempted.
  public static bool AccessDenied(uint code){return code==32769||code==0x80041003||code==0x80070005;}
  public static bool AuthorizationDenial(bool attempted,uint code){return attempted&&AccessDenied(code);}
  // Hyper-V v2 uses VirtualSystemType, not the v1 SettingType property.
  // https://learn.microsoft.com/en-us/windows/win32/hyperv_v2/msvm-settingsdefinestate
  // https://learn.microsoft.com/en-us/windows/win32/hyperv_v2/msvm-virtualsystemsettingdata
  public static bool CurrentSetting(string type,string identifier,string expected){
   return type=="Microsoft:Hyper-V:System:Realized"&&identifier!=null&&expected!=null&&
    Regex.IsMatch(identifier,"\\A[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\\z")&&
    String.Equals(identifier,expected,StringComparison.OrdinalIgnoreCase);
  }
 }
#if !PORTABLE_FIXTURE
 public sealed class Probe : ServiceBase {
  readonly Settings cfg; readonly string output; volatile bool stopped;
  public Probe(Settings value,string path){cfg=value;output=path;ServiceName=cfg.Values["service_name"];AutoLog=false;CanStop=true;ExitCode=1;}
  protected override void OnStart(string[] args){if(args.Length!=0||stopped)throw new InvalidOperationException();ThreadPool.QueueUserWorkItem(delegate{RunProbe();});}
  protected override void OnStop(){stopped=true;}
  void RunProbe(){
   bool attempted=false,complete=false,authorized=false,admin=false,role=false;uint code=0xffffffff;
   string stage="identity";
   try {
    using(var id=WindowsIdentity.GetCurrent()){
     var principal=new WindowsPrincipal(id);admin=principal.IsInRole(new SecurityIdentifier("S-1-5-32-544"));role=principal.IsInRole(new SecurityIdentifier("S-1-5-32-578"));
     if(id.User.Value!=cfg.Values["service_sid"]||Process.GetCurrentProcess().SessionId!=0||admin||role!=(cfg.Values["expected_hyperv_role"]=="true"))throw new InvalidOperationException();
    }
    stage="target_query";
    var options=new ConnectionOptions();options.EnablePrivileges=false;options.Timeout=TimeSpan.FromSeconds(5);
    var scope=new ManagementScope(@"\\.\root\virtualization\v2",options);scope.Connect();
    var query=new ObjectQuery("SELECT * FROM Msvm_ComputerSystem WHERE Name='"+cfg.Values["vm_id"]+"'");
    using(var search=new ManagementObjectSearcher(scope,query))using(var found=search.Get()){
     if(found.Count!=1)throw new InvalidOperationException();
     foreach(ManagementObject vm in found)using(vm){
      ManagementObject settings=null;
      using(var related=vm.GetRelated("Msvm_VirtualSystemSettingData","Msvm_SettingsDefineState",null,null,null,null,false,null)){
       foreach(ManagementObject candidate in related){if(Settings.CurrentSetting(candidate["VirtualSystemType"] as string,candidate["VirtualSystemIdentifier"] as string,cfg.Values["vm_id"])){if(settings!=null)throw new InvalidOperationException();settings=candidate;}else candidate.Dispose();}
      }
      if(settings==null)throw new InvalidOperationException();
      using(settings){
       string[] notes=settings["Notes"] as string[];
       if(notes==null||notes.Length!=1||notes[0]!=cfg.Marker())throw new InvalidOperationException();
       uint state=Convert.ToUInt32(vm["EnabledState"]);string operation=cfg.Values["operation"];
       if((operation=="power_on"&&state!=3)||(operation=="power_off"&&state!=2))throw new InvalidOperationException();
       stage="valid_operation";
       var invoke=new InvokeMethodOptions();invoke.Timeout=TimeSpan.FromSeconds(5);
       ManagementBaseObject answer;
       if(operation=="settings"){
        using(var serviceQuery=new ManagementObjectSearcher(scope,new ObjectQuery("SELECT * FROM Msvm_VirtualSystemManagementService")))using(var services=serviceQuery.Get()){
         if(services.Count!=1)throw new InvalidOperationException();
         answer=null;
         foreach(ManagementObject service in services)using(service){
          using(var input=service.GetMethodParameters("ModifySystemSettings")){
           settings["Notes"]=new string[]{cfg.Marker()+":tested"};input["SystemSettings"]=settings.GetText(TextFormat.CimDtd20);
           if(stopped)throw new InvalidOperationException();attempted=true;
           answer=service.InvokeMethod("ModifySystemSettings",input,invoke);
          }
         }
        }
       }else{
        using(var input=vm.GetMethodParameters("RequestStateChange")){
         input["RequestedState"]=(ushort)(operation=="power_on"?2:3);
         if(stopped)throw new InvalidOperationException();attempted=true;
         answer=vm.InvokeMethod("RequestStateChange",input,invoke);
        }
       }
       if(answer==null)throw new InvalidOperationException();
       using(answer){
        code=Convert.ToUInt32(answer["ReturnValue"]);
        if(code==4096){
         string jobPath=answer["Job"] as string;if(String.IsNullOrEmpty(jobPath))throw new InvalidOperationException();
         using(var job=new ManagementObject(scope,new ManagementPath(jobPath),null)){
          var clock=Stopwatch.StartNew();
          while(!stopped&&clock.Elapsed.TotalSeconds<10){job.Get();uint stateJob=Convert.ToUInt32(job["JobState"]);if(stateJob==7||stateJob==8||stateJob==9||stateJob==10){code=Convert.ToUInt32(job["ErrorCode"]);if(stateJob!=7&&code==0)code=0xffffffff;break;}Thread.Sleep(50);}
         }
        }
       }
       authorized=code==0;complete=authorized||Settings.AuthorizationDenial(attempted,code);
      }
     }
    }
   }catch(ManagementException error){code=unchecked((uint)error.ErrorCode);complete=Settings.AuthorizationDenial(attempted,code);}
    catch(System.Runtime.InteropServices.COMException error){code=unchecked((uint)error.ErrorCode);complete=Settings.AuthorizationDenial(attempted,code);}
    catch{complete=false;}
   if(stopped){complete=false;authorized=false;}
   try{
    var process=Process.GetCurrentProcess();
    string json="{\"schema_version\":1,\"scope\":\"owned_vm_provider_role_probe\",\"process_id\":"+process.Id+",\"process_start_ticks\":\""+process.StartTime.ToUniversalTime().Ticks+"\",\"operation\":\""+cfg.Values["operation"]+"\",\"stage\":\""+stage+"\",\"attempted\":"+attempted.ToString().ToLowerInvariant()+",\"complete\":"+complete.ToString().ToLowerInvariant()+",\"authorized\":"+authorized.ToString().ToLowerInvariant()+",\"authorization_denied\":"+Settings.AuthorizationDenial(attempted,code).ToString().ToLowerInvariant()+",\"return_code\":"+code+",\"administrator\":"+admin.ToString().ToLowerInvariant()+",\"hyperv_role\":"+role.ToString().ToLowerInvariant()+"}\n";
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
