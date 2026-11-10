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
   if(Values["format"]!="ha-provider-probe-v6"||!Regex.IsMatch(Values["service_name"],"\\AHaProbe_[0-9a-f]{16}\\z")||
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
  // COM interop maps E_ACCESSDENIED to UnauthorizedAccessException, not always
  // COMException. Fixed type names are diagnostics only; no exception grants authority.
  // https://learn.microsoft.com/en-us/dotnet/standard/exceptions/handling-com-interop-exceptions
  public static string ExceptionKind(Exception error){
   if(error==null)return "none";
   Type type=error.GetType();
#if !PORTABLE_FIXTURE
   if(type==typeof(ManagementException))return "management";
#endif
   if(type==typeof(System.Runtime.InteropServices.COMException))return "com";
   if(type==typeof(UnauthorizedAccessException))return "unauthorized_access";
   return "other";
  }
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
 public sealed class ExecEvidence {
  public bool Started,Returned,OutputPresent,OutputVerified;
  public uint ExecHresult=0xffffffff,OutputHresult=0xffffffff,ReturnValue=0xffffffff;
  public string Job;
  static bool DenialHresult(uint value){return value==0x80041003||value==0x80070005;}
  public static bool TryReturnValue(object value,int cimType,out uint result){result=0xffffffff;if(cimType!=19||(!(value is int)&&!(value is uint)))return false;result=value is int?unchecked((uint)(int)value):(uint)value;return true;}
  public static bool TryJobNumber(object value,int cimType,out uint result){result=0xffffffff;if(cimType!=18||!(value is ushort))return false;result=(ushort)value;return true;}
  public bool Denied(){
   if(!Started||!Returned)return false;
   if(DenialHresult(ExecHresult))return true;
   return ExecHresult==0&&OutputPresent&&OutputHresult==0&&OutputVerified&&Settings.AccessDenied(ReturnValue);
  }
  public bool Verified(){return Started&&Returned&&(ExecHresult>=0x80000000||(ExecHresult==0&&OutputPresent&&OutputHresult==0&&OutputVerified));}
  public uint Code(){return !Verified()?0xffffffff:ExecHresult!=0?ExecHresult:ReturnValue;}
 }
#if !PORTABLE_FIXTURE
 // Public COM ABI from WbemCli.h. Unused pointer arguments remain IntPtr;
 // methods retain their SDK order, including all unused slots. No private API.
 // https://github.com/microsoft/win32metadata/blob/main/generation/WinSDK/RecompiledIdlHeaders/um/WbemCli.h
 [System.Runtime.InteropServices.ComImport,System.Runtime.InteropServices.Guid("dc12a687-737f-11cf-884d-00aa004b2e24"),System.Runtime.InteropServices.InterfaceType(System.Runtime.InteropServices.ComInterfaceType.InterfaceIsIUnknown)]
 interface NativeLocator {
  [System.Runtime.InteropServices.PreserveSig] int ConnectServer([System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.BStr)]string resource,[System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.BStr)]string user,[System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.BStr)]string password,[System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.BStr)]string locale,int flags,[System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.BStr)]string authority,IntPtr context,out NativeServices services);
 }
 [System.Runtime.InteropServices.ComImport,System.Runtime.InteropServices.Guid("9556dc99-828c-11cf-a37e-00aa003240c7"),System.Runtime.InteropServices.InterfaceType(System.Runtime.InteropServices.ComInterfaceType.InterfaceIsIUnknown)]
 interface NativeServices {
  [System.Runtime.InteropServices.PreserveSig] int OpenNamespace([System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.BStr)]string name,int flags,IntPtr context,out IntPtr services,out IntPtr result);
  [System.Runtime.InteropServices.PreserveSig] int CancelAsyncCall(IntPtr sink);
  [System.Runtime.InteropServices.PreserveSig] int QueryObjectSink(int flags,out IntPtr sink);
  [System.Runtime.InteropServices.PreserveSig] int GetObject([System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.BStr)]string path,int flags,IntPtr context,out NativeObject obj,out NativeCallResult result);
  [System.Runtime.InteropServices.PreserveSig] int GetObjectAsync([System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.BStr)]string path,int flags,IntPtr context,IntPtr sink);
  [System.Runtime.InteropServices.PreserveSig] int PutClass(IntPtr obj,int flags,IntPtr context,out IntPtr result);
  [System.Runtime.InteropServices.PreserveSig] int PutClassAsync(IntPtr obj,int flags,IntPtr context,IntPtr sink);
  [System.Runtime.InteropServices.PreserveSig] int DeleteClass([System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.BStr)]string name,int flags,IntPtr context,out IntPtr result);
  [System.Runtime.InteropServices.PreserveSig] int DeleteClassAsync([System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.BStr)]string name,int flags,IntPtr context,IntPtr sink);
  [System.Runtime.InteropServices.PreserveSig] int CreateClassEnum([System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.BStr)]string name,int flags,IntPtr context,out IntPtr enumerator);
  [System.Runtime.InteropServices.PreserveSig] int CreateClassEnumAsync([System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.BStr)]string name,int flags,IntPtr context,IntPtr sink);
  [System.Runtime.InteropServices.PreserveSig] int PutInstance(IntPtr obj,int flags,IntPtr context,out IntPtr result);
  [System.Runtime.InteropServices.PreserveSig] int PutInstanceAsync(IntPtr obj,int flags,IntPtr context,IntPtr sink);
  [System.Runtime.InteropServices.PreserveSig] int DeleteInstance([System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.BStr)]string path,int flags,IntPtr context,out IntPtr result);
  [System.Runtime.InteropServices.PreserveSig] int DeleteInstanceAsync([System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.BStr)]string path,int flags,IntPtr context,IntPtr sink);
  [System.Runtime.InteropServices.PreserveSig] int CreateInstanceEnum([System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.BStr)]string name,int flags,IntPtr context,out IntPtr enumerator);
  [System.Runtime.InteropServices.PreserveSig] int CreateInstanceEnumAsync([System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.BStr)]string name,int flags,IntPtr context,IntPtr sink);
  [System.Runtime.InteropServices.PreserveSig] int ExecQuery([System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.BStr)]string language,[System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.BStr)]string query,int flags,IntPtr context,out IntPtr enumerator);
  [System.Runtime.InteropServices.PreserveSig] int ExecQueryAsync([System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.BStr)]string language,[System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.BStr)]string query,int flags,IntPtr context,IntPtr sink);
  [System.Runtime.InteropServices.PreserveSig] int ExecNotificationQuery([System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.BStr)]string language,[System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.BStr)]string query,int flags,IntPtr context,out IntPtr enumerator);
  [System.Runtime.InteropServices.PreserveSig] int ExecNotificationQueryAsync([System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.BStr)]string language,[System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.BStr)]string query,int flags,IntPtr context,IntPtr sink);
  // Synchronous ABI: IWbemClassObject** out and a literal NULL IWbemCallResult**.
  [System.Runtime.InteropServices.PreserveSig] int ExecMethod([System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.BStr)]string path,[System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.BStr)]string method,int flags,IntPtr context,NativeObject input,out NativeObject output,IntPtr result);
  [System.Runtime.InteropServices.PreserveSig] int ExecMethodAsync([System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.BStr)]string path,[System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.BStr)]string method,int flags,IntPtr context,IntPtr input,IntPtr sink);
 }
 [System.Runtime.InteropServices.ComImport,System.Runtime.InteropServices.Guid("dc12a681-737f-11cf-884d-00aa004b2e24"),System.Runtime.InteropServices.InterfaceType(System.Runtime.InteropServices.ComInterfaceType.InterfaceIsIUnknown)]
 interface NativeObject {
  [System.Runtime.InteropServices.PreserveSig] int GetQualifierSet(out IntPtr qualifiers);
  [System.Runtime.InteropServices.PreserveSig] int Get([System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.LPWStr)]string name,int flags,[System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.Struct)]out object value,out int type,out int flavor);
  [System.Runtime.InteropServices.PreserveSig] int Put([System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.LPWStr)]string name,int flags,[System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.Struct)]ref object value,int type);
  [System.Runtime.InteropServices.PreserveSig] int Delete([System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.LPWStr)]string name);
  [System.Runtime.InteropServices.PreserveSig] int GetNames([System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.LPWStr)]string qualifier,int flags,IntPtr value,out IntPtr names);
  [System.Runtime.InteropServices.PreserveSig] int BeginEnumeration(int flags);
  [System.Runtime.InteropServices.PreserveSig] int Next(int flags,out IntPtr name,IntPtr value,IntPtr type,IntPtr flavor);
  [System.Runtime.InteropServices.PreserveSig] int EndEnumeration();
  [System.Runtime.InteropServices.PreserveSig] int GetPropertyQualifierSet([System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.LPWStr)]string name,out IntPtr qualifiers);
  [System.Runtime.InteropServices.PreserveSig] int Clone(out IntPtr copy);
  [System.Runtime.InteropServices.PreserveSig] int GetObjectText(int flags,out IntPtr text);
  [System.Runtime.InteropServices.PreserveSig] int SpawnDerivedClass(int flags,out IntPtr obj);
  [System.Runtime.InteropServices.PreserveSig] int SpawnInstance(int flags,out NativeObject obj);
  [System.Runtime.InteropServices.PreserveSig] int CompareTo(int flags,IntPtr obj);
  [System.Runtime.InteropServices.PreserveSig] int GetPropertyOrigin([System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.LPWStr)]string name,out IntPtr origin);
  [System.Runtime.InteropServices.PreserveSig] int InheritsFrom([System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.LPWStr)]string name);
  [System.Runtime.InteropServices.PreserveSig] int GetMethod([System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.LPWStr)]string name,int flags,out NativeObject input,out NativeObject output);
  [System.Runtime.InteropServices.PreserveSig] int PutMethod([System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.LPWStr)]string name,int flags,IntPtr input,IntPtr output);
  [System.Runtime.InteropServices.PreserveSig] int DeleteMethod([System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.LPWStr)]string name);
  [System.Runtime.InteropServices.PreserveSig] int BeginMethodEnumeration(int flags);
  [System.Runtime.InteropServices.PreserveSig] int NextMethod(int flags,out IntPtr name,out IntPtr input,out IntPtr output);
  [System.Runtime.InteropServices.PreserveSig] int EndMethodEnumeration();
  [System.Runtime.InteropServices.PreserveSig] int GetMethodQualifierSet([System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.LPWStr)]string name,out IntPtr qualifiers);
  [System.Runtime.InteropServices.PreserveSig] int GetMethodOrigin([System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.LPWStr)]string name,out IntPtr origin);
 }
 [System.Runtime.InteropServices.ComImport,System.Runtime.InteropServices.Guid("44aca675-e8fc-11d0-a07c-00c04fb68820"),System.Runtime.InteropServices.InterfaceType(System.Runtime.InteropServices.ComInterfaceType.InterfaceIsIUnknown)]
 interface NativeCallResult {
  [System.Runtime.InteropServices.PreserveSig] int GetResultObject(int timeout,out NativeObject obj);
  [System.Runtime.InteropServices.PreserveSig] int GetResultString(int timeout,out IntPtr text);
  [System.Runtime.InteropServices.PreserveSig] int GetResultServices(int timeout,out IntPtr services);
  [System.Runtime.InteropServices.PreserveSig] int GetCallStatus(int timeout,out int status);
 }
 sealed class DirectWmi : IDisposable {
  NativeLocator locator;NativeServices services;NativeObject klass,inputSignature,outputSignature,input;
  readonly string path,method;public bool ProxySecurityVerified {get;private set;}
  [System.Runtime.InteropServices.DllImport("ole32.dll",ExactSpelling=true)] static extern int CoSetProxyBlanket([System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.IUnknown)]object proxy,uint authentication,uint authorization,IntPtr principal,uint authenticationLevel,uint impersonationLevel,IntPtr identity,uint capabilities);
  static void Need(bool ok){if(!ok)throw new InvalidOperationException();}
  static void Release(object obj){try{if(obj!=null&&System.Runtime.InteropServices.Marshal.IsComObject(obj))System.Runtime.InteropServices.Marshal.ReleaseComObject(obj);}catch{}}
  static int Blanket(object proxy){return CoSetProxyBlanket(proxy,10,0,IntPtr.Zero,6,3,IntPtr.Zero,0);}
  static object Get(NativeObject obj,string name,int expectedType){object value;int type,flavor;Need(obj.Get(name,0,out value,out type,out flavor)==0&&type==expectedType);return value;}
  public DirectWmi(ManagementPath target,string operation,string serialized){
   string className=operation=="settings"?"Msvm_VirtualSystemManagementService":"Msvm_ComputerSystem";
   Need(operation=="settings"||operation=="power_on"||operation=="power_off");
   Need(Settings.NativeRelativeTarget(target.IsInstance,target.ClassName,className,target.Server,target.NamespacePath,target.Path,target.RelativePath,target.RelativePath));
   path=target.RelativePath;method=operation=="settings"?"ModifySystemSettings":"RequestStateChange";
   try{
    locator=(NativeLocator)Activator.CreateInstance(Type.GetTypeFromCLSID(new Guid("4590f811-1d3a-11d0-891f-00aa004b2e24"),true));
    // Null credential/authority fields select this service's current identity.
    Need(locator.ConnectServer(@"root\virtualization\v2",null,null,null,0,null,IntPtr.Zero,out services)==0&&services!=null);
    Need(Blanket(services)==0);ProxySecurityVerified=true;
    NativeObject immediate=null;NativeCallResult metadata=null;
    try{
     // Class metadata only; no GetObject/association query on the VM instance.
     Need(services.GetObject(className,0x10,IntPtr.Zero,out immediate,out metadata)==0&&metadata!=null);
     Need(Blanket(metadata)==0);int status;
     Need(metadata.GetCallStatus(5000,out status)==0&&status==0);
     Need(metadata.GetResultObject(0,out klass)==0&&klass!=null);
    }finally{Release(immediate);Release(metadata);}
    Need((Get(klass,"__CLASS",8) as string)==className);
    Need(klass.GetMethod(method,0,out inputSignature,out outputSignature)==0&&inputSignature!=null&&outputSignature!=null);
    Get(outputSignature,"ReturnValue",19);
    string property=operation=="settings"?"SystemSettings":"RequestedState";int type=operation=="settings"?8:18;
    Get(inputSignature,property,type);
    Need(inputSignature.SpawnInstance(0,out input)==0&&input!=null);
    object value=operation=="settings"?(object)serialized:(object)(operation=="power_on"?2:3);
    Need(operation!="settings"||(!String.IsNullOrEmpty(serialized)&&serialized.Length<=1048576));
    Need(input.Put(property,0,ref value,0)==0);
    object actual=Get(input,property,type);
    Need(operation=="settings"?actual is string&&(string)actual==serialized:actual is int&&(int)actual==(operation=="power_on"?2:3));
   }catch{Dispose();throw;}
  }
  public void Execute(ExecEvidence trace,Action beforeExec){
   NativeObject output=null;
   try{
    // This RPC is synchronous. An outer process deadline or process exit does
    // not cancel provider work; an incomplete call requires live reconciliation.
    // https://learn.microsoft.com/en-us/windows/win32/api/wbemcli/nf-wbemcli-iwbemservices-execmethod
    beforeExec();trace.Started=true;
    int hr=services.ExecMethod(path,method,0,IntPtr.Zero,input,out output,IntPtr.Zero);
    trace.Returned=true;trace.ExecHresult=unchecked((uint)hr);
    trace.OutputPresent=output!=null;
    if(hr!=0||output==null)return;
    object value;int type,flavor;
    hr=output.Get("ReturnValue",0,out value,out type,out flavor);trace.OutputHresult=unchecked((uint)hr);
    uint typedValue;if(hr!=0||!ExecEvidence.TryReturnValue(value,type,out typedValue))return;
    trace.ReturnValue=typedValue;trace.OutputVerified=true;
    if(trace.ReturnValue==4096){object job=Get(output,"Job",102);trace.Job=job as string;if(String.IsNullOrEmpty(trace.Job)||trace.Job.Length>4096)trace.OutputVerified=false;}
   }finally{Release(output);}
  }
  public void Dispose(){Release(input);Release(inputSignature);Release(outputSignature);Release(klass);Release(services);Release(locator);input=null;inputSignature=null;outputSignature=null;klass=null;services=null;locator=null;}
 }

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
  static ManagementPath BoundJobPath(string text){
   if(String.IsNullOrEmpty(text)||text.Length>4096)throw new InvalidOperationException();
   var path=new ManagementPath(text);
   if(!path.IsInstance||path.ClassName!="Msvm_ConcreteJob"||
      (!String.IsNullOrEmpty(path.Server)&&path.Server!="."&&!String.Equals(path.Server,Environment.MachineName,StringComparison.OrdinalIgnoreCase))||
      (!String.IsNullOrEmpty(path.NamespacePath)&&!String.Equals(path.NamespacePath,@"root\virtualization\v2",StringComparison.OrdinalIgnoreCase)))throw new InvalidOperationException();
   return new ManagementPath(path.RelativePath);
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
   var native=new ExecEvidence();bool proxySecurity=false,jobVerified=false;string jobHash="";uint? jobState=null,jobError=null;
   string stage="identity",exceptionKind="none",exceptionScope="none";uint exceptionHresult=0;
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
    var scope=new ManagementScope(@"\\.\root\virtualization\v2",options);
    using(var direct=new DirectWmi(operation=="settings"?servicePath:vmPath,operation,serialized)){
     proxySecurity=direct.ProxySecurityVerified;
     if(stopped)throw new InvalidOperationException();
     stage="valid_operation";
     // PreserveSig returns native HRESULTs without CLR exception projection.
     // No managed target object/GetMethodParameters/implicit GetObject exists.
     try{direct.Execute(native,delegate{if(stopped)throw new InvalidOperationException();});}
     catch(Exception error){exceptionKind=Settings.ExceptionKind(error);exceptionHresult=unchecked((uint)error.HResult);exceptionScope=native.Started?"invoke":"outside_invoke";}
     attempted=native.Started;methodResult=native.Verified();code=native.Code();denied=exceptionScope=="none"&&native.Denied();complete=denied;
     if(methodResult&&!denied){
      if(code==4096){
       var jobPath=BoundJobPath(native.Job);jobHash=Settings.Hash(Encoding.UTF8.GetBytes(jobPath.RelativePath));
       using(var job=new ManagementObject(scope,jobPath,new ObjectGetOptions(null,TimeSpan.FromSeconds(2),false))){
        var clock=Stopwatch.StartNew();
        // Provider 4096 is not completion. Bind the job to this exact VM and
        // observe its terminal success separately; never invoke the method again.
        while(!stopped&&clock.Elapsed.TotalSeconds<10){
         job.Get();if(job.Path.RelativePath!=jobPath.RelativePath)throw new InvalidOperationException();
         using(var affected=job.GetRelated(
          relatedClass:"Msvm_ComputerSystem",relationshipClass:"Msvm_AffectedJobElement",
          relationshipQualifier:null,relatedQualifier:null,
          relatedRole:"AffectedElement",thisRole:"AffectingElement",
          classDefinitionsOnly:false,options:null)){
          if(affected.Count!=1)throw new InvalidOperationException();
          foreach(ManagementObject vm in affected)using(vm){if(!String.Equals(Convert.ToString(vm["Name"]),cfg.Values["vm_id"],StringComparison.OrdinalIgnoreCase))throw new InvalidOperationException();}
         }
         uint number;
         if(!ExecEvidence.TryJobNumber(job["JobState"],(int)job.Properties["JobState"].Type,out number))throw new InvalidOperationException();jobState=number;
         if(jobState==7||jobState==8||jobState==9||jobState==10){
          if(!ExecEvidence.TryJobNumber(job["ErrorCode"],(int)job.Properties["ErrorCode"].Type,out number))throw new InvalidOperationException();jobError=number;
          jobVerified=jobState==7&&jobError==0;code=jobVerified?0:0xffffffff;break;
         }
         Thread.Sleep(50);
        }
       }
       if(!jobVerified)code=0xffffffff;
      }
      authorized=code==0&&exceptionScope=="none";complete=authorized||denied;
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
   }catch(Exception error){
    exceptionKind=Settings.ExceptionKind(error);exceptionHresult=unchecked((uint)error.HResult);exceptionScope="outside_invoke";
    denied=false;authorized=false;complete=false;
   }
   if(stopped){complete=false;authorized=false;denied=false;}
   try{
    var process=Process.GetCurrentProcess();
    string json="{\"schema_version\":6,\"scope\":\"owned_vm_provider_role_probe\",\"process_id\":"+process.Id+",\"process_start_ticks\":\""+process.StartTime.ToUniversalTime().Ticks+"\",\"operation\":\""+cfg.Values["operation"]+"\",\"stage\":\""+stage+"\",\"attempted\":"+attempted.ToString().ToLowerInvariant()+",\"complete\":"+complete.ToString().ToLowerInvariant()+",\"authorized\":"+authorized.ToString().ToLowerInvariant()+",\"authorization_denied\":"+denied.ToString().ToLowerInvariant()+",\"return_code\":"+code+",\"administrator\":"+admin.ToString().ToLowerInvariant()+",\"hyperv_role\":"+role.ToString().ToLowerInvariant()+",\"settings_serialized_value_present\":"+settingsSerialized.ToString().ToLowerInvariant()+",\"settings_readback_verified\":"+settingsReadback.ToString().ToLowerInvariant()+",\"native_target_paths_bound\":"+pathsBound.ToString().ToLowerInvariant()+",\"method_result_verified\":"+methodResult.ToString().ToLowerInvariant()+",\"target_descriptor_sha256\":\""+cfg.Values["target_descriptor_sha256"]+"\",\"settings_payload_sha256\":\""+(cfg.Descriptor==null?"":cfg.Descriptor.Values["settings_payload_sha256"])+"\",\"settings_current_path_sha256\":\""+currentPathHash+"\",\"settings_current_instance_sha256\":\""+currentInstanceHash+"\",\"settings_current_association_verified\":"+currentAssociation.ToString().ToLowerInvariant()+",\"exception_kind\":\""+exceptionKind+"\",\"exception_scope\":\""+exceptionScope+"\",\"exception_hresult\":"+exceptionHresult+",\"native_proxy_security_verified\":"+proxySecurity.ToString().ToLowerInvariant()+",\"native_exec_returned\":"+native.Returned.ToString().ToLowerInvariant()+",\"native_exec_hresult\":"+native.ExecHresult+",\"native_call_status_hresult\":"+"null"+",\"native_call_status_terminal\":"+"null"+",\"native_call_status\":"+"null"+",\"native_result_hresult\":"+native.OutputHresult+",\"native_output_verified\":"+native.OutputVerified.ToString().ToLowerInvariant()+",\"native_call_result_present\":"+"null"+",\"native_call_proxy_hresult\":null,\"call_mode\":\"synchronous\",\"call_result_applicability\":\"not_used\",\"native_output_present\":"+native.OutputPresent.ToString().ToLowerInvariant()+",\"method_return_value\":"+(native.OutputVerified?native.ReturnValue.ToString():"null")+",\"job_completion_verified\":"+jobVerified.ToString().ToLowerInvariant()+",\"job_path_sha256\":\""+jobHash+"\",\"job_state\":"+(jobState.HasValue?jobState.Value.ToString():"null")+",\"job_error_code\":"+(jobError.HasValue?jobError.Value.ToString():"null")+"}\n";
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
