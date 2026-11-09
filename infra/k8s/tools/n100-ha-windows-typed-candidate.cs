// Disconnected read-only diagnostic; never a guard or an authority receipt.
// API contracts: https://learn.microsoft.com/en-us/windows/win32/taskschd/taskfolder-gettasks
// https://learn.microsoft.com/en-us/windows/win32/taskschd/registeredtask-xml
// https://learn.microsoft.com/en-us/windows/win32/taskschd/task-scheduler-schema
using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Xml;

namespace N100HaTypedCandidateV1 {
    // Closed diagnostic labels, never provider text or a root-cause assertion.
    public enum FailurePhase { none, wmi_locator, wmi_connect, task_service, task_connect,
        services_query, services_enumerator, services_fields, task_read,
        processes_query, processes_enumerator, processes_fields,
        boot_query, boot_enumerator, boot_fields, cleanup, unknown }
    public sealed class Blocked : Exception {
        public readonly string Reason;
        public Blocked(string reason) : base(reason) { Reason = reason; }
    }
    public sealed class Budget {
        readonly Stopwatch watch = Stopwatch.StartNew();
        readonly int milliseconds;
        int characters;
        FailurePhase first=N100HaTypedCandidateV1.FailurePhase.none;
        public int Actions;
        public string FailurePhase {get{return first.ToString();}}
        internal void Capture(FailurePhase phase) {if(first==N100HaTypedCandidateV1.FailurePhase.none)first=phase;}
        internal T At<T>(FailurePhase phase,Func<T> operation) {try{return operation();}catch {Capture(phase);throw;}}
        internal void At(FailurePhase phase,Action operation) {try{operation();}catch {Capture(phase);throw;}}
        public Budget(int limit) { if(limit < 1 || limit > 2000) throw new Blocked("input_rejected"); milliseconds=limit; }
        public double Elapsed { get { return watch.Elapsed.TotalMilliseconds; } }
        public void Check() { if(Elapsed >= milliseconds) throw new Blocked("deadline_exceeded"); }
        public void Text(string text, int individualLimit) {
            if(text == null || text.Length > individualLimit || text.Length > 2097152-characters) throw new Blocked("unsupported_metadata");
            characters += text.Length;
        }
    }
    public interface ITask : IDisposable {
        string Name { get; }
        string Path { get; }
        string Xml { get; }
        int State { get; }
    }
    public interface IFolder : IDisposable {
        string Path { get; }
        IList<ITask> GetTasks(int flags);
        IList<IFolder> GetFolders(int flags);
    }
    public sealed class ActionRecord {
        public string Execute { get; set; }
        public string Arguments { get; set; }
        public string WorkingDirectory { get; set; }
        public string ClassId { get; set; }
    }
    public sealed class TaskSettings { public bool Enabled { get; set; } }
    public sealed class TaskRecord {
        public string TaskName { get; set; }
        public string TaskPath { get; set; }
        public string State { get; set; }
        public TaskSettings Settings { get; set; }
        public List<ActionRecord> Actions { get; set; }
        public string CandidateXml { get; set; }
    }
    public sealed class Measurement {
        public string FailurePhase {get;internal set;}
        public string PhaseSemantics {get{return "first-caught-phase";}}
        public string Status { get; set; }
        public string Reason { get; set; }
        public int? TaskCount { get; set; }
        public int? ActionCount { get; set; }
        public double ElapsedMilliseconds { get; set; }
    }
    public enum WmiKind { Services, Processes, Boot }
    public sealed class WmiProperty {
        public readonly object Value;public readonly int CimType;public readonly bool IsArray;
        public WmiProperty(object value,int cimType,bool isArray) { Value=value;CimType=cimType;IsArray=isArray; }
    }
    public interface IWmiRow : IDisposable { WmiProperty Read(string name); }
    public interface IWmiRows : IEnumerable<IWmiRow>, IDisposable { }
    public interface IWmiReader : IDisposable { IWmiRows Query(WmiKind kind); }
    public sealed class ServiceRecord {
        public string Name {get;set;}public string PathName {get;set;}public string StartName {get;set;}
        public string StartMode {get;set;}public string ServiceType {get;set;}public string State {get;set;}
        public uint? ProcessId {get;set;}
    }
    public sealed class ProcessRecord {
        public uint? ProcessId {get;set;}public uint? ParentProcessId {get;set;}
        public string Name {get;set;}public string CommandLine {get;set;}public string ExecutablePath {get;set;}
        public DateTime? CreationDate {get;set;}public long? CreationDateUtcTicks {get;set;}public uint? SessionId {get;set;}
    }
    public sealed class BootRecord { public DateTime LastBootUpTime {get;set;}public long UtcTicks {get;set;} }
    public sealed class BasicRecords {
        public List<ServiceRecord> Services {get;set;}public List<TaskRecord> Tasks {get;set;}
        public List<ProcessRecord> Processes {get;set;}public BootRecord Boot {get;set;}
    }
    public sealed class BasicMeasurement {
        public string FailurePhase {get;internal set;}
        public string PhaseSemantics {get{return "first-caught-phase";}}
        public string Status {get;set;}public string Reason {get;set;}
        public int? ServiceCount {get;set;}public int? TaskCount {get;set;}public int? ActionCount {get;set;}
        public int? ProcessCount {get;set;}public int? BootCount {get;set;}public double ElapsedMilliseconds {get;set;}
    }
    public static class Collector {
        const string Ns="http://schemas.microsoft.com/windows/2004/02/mit/task";
        static void Unsupported() { throw new Blocked("unsupported_metadata"); }
        static void DisposeAll(IEnumerable<IDisposable> values) {
            bool failed=false;
            foreach(var value in values) { try { if(value!=null) value.Dispose(); } catch {failed=true;} }
            if(failed) throw new Blocked("query_failed");
        }
        // Same local WMI classes as the original CIM cmdlets; no remote endpoint,
        // caller WQL, method execution, privilege changes or Count enumeration.
        // https://learn.microsoft.com/en-us/windows/win32/wmisdk/swbemservices-execquery
        // https://learn.microsoft.com/en-us/windows/win32/wmisdk/numbers
        // https://learn.microsoft.com/en-us/windows/win32/wmisdk/cim-datetime
        static readonly string[] ServiceFields={"Name","PathName","StartName","StartMode","ServiceType","State","ProcessId"};
        static readonly string[] ProcessFields={"ProcessId","ParentProcessId","Name","CommandLine","ExecutablePath","CreationDate","SessionId"};
        static readonly string[] BootFields={"LastBootUpTime"};
        static string[] Fields(WmiKind kind) {
            switch(kind) {case WmiKind.Services:return ServiceFields;case WmiKind.Processes:return ProcessFields;case WmiKind.Boot:return BootFields;default:throw new Blocked("unsupported_metadata");}
        }
        static FailurePhase QueryPhase(WmiKind kind){return kind==WmiKind.Services ? FailurePhase.services_query : kind==WmiKind.Processes ? FailurePhase.processes_query : FailurePhase.boot_query;}
        static FailurePhase EnumPhase(WmiKind kind){return kind==WmiKind.Services ? FailurePhase.services_enumerator : kind==WmiKind.Processes ? FailurePhase.processes_enumerator : FailurePhase.boot_enumerator;}
        static FailurePhase FieldPhase(WmiKind kind){return kind==WmiKind.Services ? FailurePhase.services_fields : kind==WmiKind.Processes ? FailurePhase.processes_fields : FailurePhase.boot_fields;}
        static WmiProperty Property(IWmiRow row,string name,int type,Budget budget) {
            budget.Check();WmiProperty property;
            try { property=row.Read(name); } catch(Blocked){throw;} catch {throw new Blocked("query_failed");}
            budget.Check();
            if(property==null || property.CimType!=type || property.IsArray) Unsupported();
            return property;
        }
        static string StringValue(IWmiRow row,string name,Budget budget) {
            object value=Property(row,name,8,budget).Value;
            if(value==null || value==DBNull.Value) return null;
            string text=value as string;if(text==null) Unsupported();budget.Text(text,65536);return text;
        }
        static uint? UInt32Value(IWmiRow row,string name,Budget budget) {
            object value=Property(row,name,19,budget).Value;
            if(value==null || value==DBNull.Value) return null;
            if(value is UInt32) return (UInt32)value;
            // WMI Automation maps CIM uint32 to VT_I4, including its high bit.
            if(value is Int32) return unchecked((UInt32)(Int32)value);
            Unsupported();return null;
        }
        public static DateTime? DmtfUtc(WmiProperty property,Budget budget) {
            budget.Check();
            if(property==null || property.CimType!=101 || property.IsArray) Unsupported();
            object value=property.Value;if(value==null || value==DBNull.Value) return null;
            string text=value as string;
            if(text==null || text.Length!=25 || text[14]!='.' || (text[21]!='+' && text[21]!='-')) Unsupported();
            budget.Text(text,25);
            for(int i=0;i<text.Length;i++) if(i!=14 && i!=21 && (text[i]<'0' || text[i]>'9')) Unsupported();
            try {
                DateTime local=DateTime.ParseExact(text.Substring(0,14),"yyyyMMddHHmmss",CultureInfo.InvariantCulture,DateTimeStyles.None);
                long micros=Int64.Parse(text.Substring(15,6),CultureInfo.InvariantCulture);
                long offset=Int64.Parse(text.Substring(22,3),CultureInfo.InvariantCulture)*TimeSpan.TicksPerMinute;
                if(text[21]=='-') offset=-offset;
                long ticks=checked(local.Ticks+micros*10-offset);
                var utc=new DateTime(ticks,DateTimeKind.Utc);budget.Check();return utc;
            } catch(Blocked){throw;}catch {Unsupported();return null;}
        }
        static ServiceRecord ProjectService(IWmiRow row,Budget budget) {
            return new ServiceRecord {Name=StringValue(row,"Name",budget),PathName=StringValue(row,"PathName",budget),StartName=StringValue(row,"StartName",budget),StartMode=StringValue(row,"StartMode",budget),ServiceType=StringValue(row,"ServiceType",budget),State=StringValue(row,"State",budget),ProcessId=UInt32Value(row,"ProcessId",budget)};
        }
        static ProcessRecord ProjectProcess(IWmiRow row,Budget budget) {
            var result=new ProcessRecord();
            result.ProcessId=UInt32Value(row,"ProcessId",budget);result.ParentProcessId=UInt32Value(row,"ParentProcessId",budget);
            result.Name=StringValue(row,"Name",budget);result.CommandLine=StringValue(row,"CommandLine",budget);result.ExecutablePath=StringValue(row,"ExecutablePath",budget);
            result.CreationDate=DmtfUtc(Property(row,"CreationDate",101,budget),budget);
            result.CreationDateUtcTicks=result.CreationDate.HasValue ? (long?)result.CreationDate.Value.Ticks : null;
            result.SessionId=UInt32Value(row,"SessionId",budget);return result;
        }
        static BootRecord ProjectBoot(IWmiRow row,Budget budget) {
            DateTime? value=DmtfUtc(Property(row,"LastBootUpTime",101,budget),budget);
            if(!value.HasValue) Unsupported();return new BootRecord {LastBootUpTime=value.Value,UtcTicks=value.Value.Ticks};
        }
        static List<T> Rows<T>(IWmiReader reader,WmiKind kind,Budget budget,Func<IWmiRow,Budget,T> project) {
            IWmiRows rows=null;IEnumerator<IWmiRow> iterator=null;var result=new List<T>();
            try {
                budget.At(QueryPhase(kind),delegate {budget.Check();rows=reader.Query(kind);budget.Check();});
                budget.At(EnumPhase(kind),delegate {iterator=rows.GetEnumerator();budget.Check();});
                while(true) {
                    bool more=budget.At(EnumPhase(kind),delegate {budget.Check();bool value=iterator.MoveNext();budget.Check();return value;});if(!more) break;
                    var row=budget.At(EnumPhase(kind),delegate{return iterator.Current;});
                    try {budget.At(FieldPhase(kind),delegate {budget.Check();if(result.Count>=4096) Unsupported();result.Add(project(row,budget));budget.Check();});}
                    finally {budget.At(FailurePhase.cleanup,delegate {if(row!=null) row.Dispose();});}
                }
            } catch(Blocked){throw;}catch {throw new Blocked("query_failed");}
            finally {budget.At(FailurePhase.cleanup,delegate {DisposeAll(new IDisposable[]{iterator,rows});});}
            budget.At(EnumPhase(kind),delegate {budget.Check();});return result;
        }
        public static BasicRecords ReadBasic(IWmiReader reader,IFolder tasks,Budget budget) {
            // Basic raw batch only: registry/code/signature/startup/identity and
            // the original filtered self/parent re-queries are NOT replaced.
            bool tasksStarted=false;
            try {
                var services=Rows(reader,WmiKind.Services,budget,ProjectService);
                budget.At(FailurePhase.services_fields,delegate {services.Sort(delegate(ServiceRecord a,ServiceRecord b){return StringComparer.CurrentCulture.Compare(a.Name,b.Name);});budget.Check();});
                tasksStarted=true;var taskRecords=ReadAll(tasks,budget);
                var processes=Rows(reader,WmiKind.Processes,budget,ProjectProcess);
                budget.At(FailurePhase.processes_fields,delegate {processes.Sort(delegate(ProcessRecord a,ProcessRecord b){return Nullable.Compare(a.ProcessId,b.ProcessId);});budget.Check();});
                var boot=Rows(reader,WmiKind.Boot,budget,ProjectBoot);budget.At(FailurePhase.boot_fields,delegate {if(boot.Count!=1) Unsupported();budget.Check();});
                return new BasicRecords {Services=services,Tasks=taskRecords,Processes=processes,Boot=boot[0]};
            } catch {budget.Capture(FailurePhase.unknown);throw;}
            finally {budget.At(FailurePhase.cleanup,delegate {if(!tasksStarted && tasks!=null)tasks.Dispose();});}
        }
        static List<XmlElement> Elements(XmlNode node) {
            var result=new List<XmlElement>();
            foreach(XmlNode child in node.ChildNodes) {
                var element=child as XmlElement;
                if(element != null) result.Add(element);
                else if((child is XmlText || child is XmlCDataSection) && !String.IsNullOrWhiteSpace(child.Value)) Unsupported();
            }
            return result;
        }
        static Dictionary<string,XmlElement> Unique(XmlNode node, string[] allowed) {
            var result=new Dictionary<string,XmlElement>(StringComparer.Ordinal);
            foreach(var child in Elements(node)) {
                if(Array.IndexOf(allowed,child.LocalName)<0 || result.ContainsKey(child.LocalName)) Unsupported();
                result.Add(child.LocalName,child);
            }
            return result;
        }
        public static TaskRecord Project(ITask task, string folder, Budget budget) {
            budget.Check();
            string name=task.Name, path=task.Path;
            if(String.IsNullOrWhiteSpace(name) || name=="." || name==".." || folder==null || !folder.StartsWith("\\",StringComparison.Ordinal) || folder.Contains("\\\\")) Unsupported();
            foreach(char c in name) if(c=='\\' || c=='/' || c<32) Unsupported();
            foreach(char c in folder) if(c=='/' || c<32) Unsupported();
            foreach(string part in folder.Split('\\')) if(part=="." || part=="..") Unsupported();
            if(!folder.EndsWith("\\",StringComparison.Ordinal)) folder+="\\";
            if(!String.Equals(path,folder+name,StringComparison.OrdinalIgnoreCase)) Unsupported();
            string xml=task.Xml;
            budget.Text(name,65536); budget.Text(path,65536); budget.Text(folder,65536); budget.Text(xml,1048576);
            int state=task.State;
            string[] states={"Unknown","Disabled","Queued","Ready","Running"};
            if(state<0 || state>=states.Length) Unsupported();
            var document=new XmlDocument { XmlResolver=null, PreserveWhitespace=true };
            try {
                var settings=new XmlReaderSettings { DtdProcessing=DtdProcessing.Prohibit, XmlResolver=null, MaxCharactersInDocument=1048576, MaxCharactersFromEntities=0 };
                using(var text=new StringReader(xml)) using(var reader=XmlReader.Create(text,settings)) document.Load(reader);
            } catch { Unsupported(); }
            budget.Check();
            var root=document.DocumentElement;
            if(root==null || root.LocalName!="Task" || root.NamespaceURI!=Ns) Unsupported();
            budget.Check();
            if(document.GetElementsByTagName("*").Count>32768) Unsupported();
            var foreign=document.SelectSingleNode("//*[namespace-uri() != 'http://schemas.microsoft.com/windows/2004/02/mit/task']");
            budget.Check();
            if(foreign!=null) Unsupported();
            var sections=Unique(root,new[]{"RegistrationInfo","Triggers","Principals","Settings","Data","Actions"});
            bool enabled=true;
            XmlElement settingsNode;
            if(sections.TryGetValue("Settings",out settingsNode)) {
                XmlElement enabledNode=null;
                foreach(var node in Elements(settingsNode)) if(node.LocalName=="Enabled") { if(enabledNode!=null) Unsupported(); enabledNode=node; }
                if(enabledNode!=null) {
                    if(enabledNode.SelectNodes("*").Count!=0) Unsupported();
                    try { enabled=XmlConvert.ToBoolean(enabledNode.InnerText.Trim()); } catch { Unsupported(); }
                }
            }
            XmlElement actionsNode;
            if(!sections.TryGetValue("Actions",out actionsNode)) { Unsupported(); return null; }
            var actions=new List<ActionRecord>();
            foreach(var node in Elements(actionsNode)) {
                budget.Check();
                if(actions.Count>=1024 || budget.Actions>=4096) Unsupported();
                budget.Actions++;
                bool exec=node.LocalName=="Exec";
                if(!exec && node.LocalName!="ComHandler") Unsupported();
                var fields=Unique(node,exec ? new[]{"Command","Arguments","WorkingDirectory"} : new[]{"ClassId","Data"});
                var values=new Dictionary<string,string>(StringComparer.Ordinal);
                foreach(var pair in fields) {
                    if(pair.Value.SelectNodes("*").Count!=0) Unsupported();
                    string value=pair.Value.InnerText; budget.Text(value,65536); values.Add(pair.Key,value);
                }
                string required=exec ? "Command" : "ClassId";
                if(!values.ContainsKey(required) || String.IsNullOrEmpty(values[required])) Unsupported();
                var action=new ActionRecord();
                if(exec) { action.Execute=values["Command"]; action.Arguments=values.ContainsKey("Arguments") ? values["Arguments"] : ""; action.WorkingDirectory=values.ContainsKey("WorkingDirectory") ? values["WorkingDirectory"] : ""; }
                else action.ClassId=values["ClassId"];
                actions.Add(action); budget.Check();
            }
            if(actions.Count==0) Unsupported();
            budget.Check();
            return new TaskRecord { TaskName=name,TaskPath=folder,State=states[state],Settings=new TaskSettings { Enabled=enabled },Actions=actions,CandidateXml=xml };
        }
        public static List<TaskRecord> ReadAll(IFolder root, Budget budget) {
            var pending=new Queue<IFolder>();pending.Enqueue(root);
            var folders=new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            var keys=new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            var result=new List<TaskRecord>();
            try {
                while(pending.Count>0) {
                    budget.Check();
                    var folder=pending.Dequeue();
                    try {
                        string path=folder.Path;budget.Text(path,65536);
                        if(folders.Count>=4096 || !path.StartsWith("\\",StringComparison.Ordinal) || !folders.Add(path)) Unsupported();
                        var tasks=folder.GetTasks(1);
                        try {
                            budget.Check();
                            foreach(var task in tasks) {
                                budget.Check();
                                var record=Project(task,path,budget);
                                if(result.Count>=4096 || !keys.Add(record.TaskPath+record.TaskName)) Unsupported();
                                result.Add(record);budget.Check();
                            }
                        } catch {budget.Capture(FailurePhase.task_read);throw;}
                        finally {budget.At(FailurePhase.cleanup,delegate {DisposeAll(tasks);});}
                        var children=folder.GetFolders(0);
                        try {
                            budget.Check();
                            foreach(var child in children) {budget.Check();if(pending.Count>=4096) Unsupported();pending.Enqueue(child);}
                        } catch {
                            var unqueued=new List<IDisposable>();foreach(var child in children) if(!pending.Contains(child)) unqueued.Add(child);
                            budget.Capture(FailurePhase.task_read);budget.At(FailurePhase.cleanup,delegate {DisposeAll(unqueued);});throw;
                        }
                    } catch {budget.Capture(FailurePhase.task_read);throw;}
                    finally {budget.At(FailurePhase.cleanup,delegate {folder.Dispose();});}
                }
                result.Sort(delegate(TaskRecord a,TaskRecord b) { int order=StringComparer.CurrentCulture.Compare(a.TaskPath,b.TaskPath);return order!=0 ? order : StringComparer.CurrentCulture.Compare(a.TaskName,b.TaskName); });
            } catch {budget.Capture(FailurePhase.task_read);throw;}
            finally {
                // Drain first so one failed Dispose cannot strand later handles.
                var remaining=new List<IDisposable>();while(pending.Count>0) remaining.Add(pending.Dequeue());budget.At(FailurePhase.cleanup,delegate {DisposeAll(remaining);});
            }
            budget.At(FailurePhase.task_read,delegate {budget.Check();});return result;
        }
        // Reflection is restricted to these fixed read members; no caller member
        // names, host, credentials, task execution or registration APIs exist.
        static object Get(object value,string name) { return value.GetType().InvokeMember(name,BindingFlags.GetProperty,null,value,null,CultureInfo.InvariantCulture); }
        static object Call(object value,string name,params object[] args) { return value.GetType().InvokeMember(name,BindingFlags.InvokeMethod|BindingFlags.OptionalParamBinding,null,value,args,CultureInfo.InvariantCulture); }
        static object Item(object value,int index) { return value.GetType().InvokeMember("Item",BindingFlags.GetProperty,null,value,new object[]{index},CultureInfo.InvariantCulture); }
        static void Release(object value) { if(value!=null && Marshal.IsComObject(value)) Marshal.FinalReleaseComObject(value); }
        static void ReleaseAll(params object[] values) {
            bool failed=false;foreach(var value in values)try {Release(value);}catch {failed=true;}
            if(failed)throw new Blocked("query_failed");
        }
        sealed class NativeWmiRow : IWmiRow {
            object value;readonly WmiKind kind;readonly Budget budget;
            public NativeWmiRow(object item,WmiKind queryKind,Budget requestBudget){value=item;kind=queryKind;budget=requestBudget;}
            public WmiProperty Read(string name) {
                if(Array.IndexOf(Fields(kind),name)<0) Unsupported();
                object properties=null,property=null;
                try {
                    budget.Check();properties=Get(value,"Properties_");budget.Check();
                    property=properties.GetType().InvokeMember("Item",BindingFlags.GetProperty|BindingFlags.InvokeMethod|BindingFlags.OptionalParamBinding,null,properties,new object[]{name},CultureInfo.InvariantCulture);budget.Check();
                    int type=Convert.ToInt32(Get(property,"CIMType"),CultureInfo.InvariantCulture);budget.Check();
                    object array=Get(property,"IsArray");budget.Check();
                    int expected=(name=="ProcessId" || name=="ParentProcessId" || name=="SessionId") ? 19 : (name=="CreationDate" || name=="LastBootUpTime") ? 101 : 8;
                    if(type!=expected || !(array is bool) || (bool)array) Unsupported();
                    object data=Get(property,"Value");budget.Check();
                    if(data!=null && Marshal.IsComObject(data)){budget.At(FailurePhase.cleanup,delegate {Release(data);});Unsupported();}
                    return new WmiProperty(data,type,false);
                } catch {budget.Capture(FieldPhase(kind));throw;}
                finally {budget.At(FailurePhase.cleanup,delegate {ReleaseAll(property,properties);});}
            }
            public void Dispose(){var old=value;value=null;Release(old);}
        }
        sealed class NativeWmiRows : IWmiRows {
            object value;readonly WmiKind kind;readonly Budget budget;bool enumerated;
            public NativeWmiRows(object rows,WmiKind queryKind,Budget requestBudget){value=rows;kind=queryKind;budget=requestBudget;}
            public IEnumerator<IWmiRow> GetEnumerator(){
                return budget.At(EnumPhase(kind),delegate {
                    if(enumerated) Unsupported();enumerated=true;budget.Check();
                    var iterator=((IEnumerable)value).GetEnumerator();
                    try {budget.Check();return (IEnumerator<IWmiRow>)new NativeWmiIterator(iterator,kind,budget);}
                    catch {budget.Capture(EnumPhase(kind));budget.At(FailurePhase.cleanup,delegate {Release(iterator);});throw;}
                });
            }
            IEnumerator IEnumerable.GetEnumerator(){return GetEnumerator();}
            public void Dispose(){var old=value;value=null;Release(old);}
        }
        sealed class NativeWmiIterator : IEnumerator<IWmiRow> {
            IEnumerator iterator;IWmiRow current;readonly WmiKind kind;readonly Budget budget;
            public NativeWmiIterator(IEnumerator source,WmiKind queryKind,Budget requestBudget){iterator=source;kind=queryKind;budget=requestBudget;}
            public IWmiRow Current {get{return current;}}
            object IEnumerator.Current {get{return Current;}}
            public bool MoveNext(){
                if(current!=null){budget.At(FailurePhase.cleanup,delegate {current.Dispose();current=null;});}
                return budget.At(EnumPhase(kind),delegate {
                budget.Check();bool more=iterator.MoveNext();
                // Retain the returned COM row before the deadline check so the
                // finally path can release a late successful MoveNext result.
                if(more) current=new NativeWmiRow(iterator.Current,kind,budget);
                budget.Check();return more;
                });
            }
            public void Reset(){throw new Blocked("unsupported_metadata");}
            public void Dispose(){
                var row=current;current=null;var old=iterator;iterator=null;
                try {if(row!=null)row.Dispose();}finally {try {var disposable=old as IDisposable;if(disposable!=null)disposable.Dispose();}finally {Release(old);}}
            }
        }
        sealed class NativeWmiReader : IWmiReader {
            object locator,service;readonly Budget budget;
            public NativeWmiReader(Budget requestBudget){
                budget=requestBudget;
                try {
                    budget.At(FailurePhase.wmi_locator,delegate {budget.Check();locator=Activator.CreateInstance(Type.GetTypeFromProgID("WbemScripting.SWbemLocator",true));budget.Check();});
                    budget.At(FailurePhase.wmi_connect,delegate {service=Call(locator,"ConnectServer",".","root\\cimv2");budget.Check();});
                } catch {budget.Capture(FailurePhase.unknown);budget.At(FailurePhase.cleanup,delegate {Dispose();});throw;}
            }
            public IWmiRows Query(WmiKind kind){
                string query;
                switch(kind){
                    case WmiKind.Services:query="SELECT Name,PathName,StartName,StartMode,ServiceType,State,ProcessId FROM Win32_Service";break;
                    case WmiKind.Processes:query="SELECT ProcessId,ParentProcessId,Name,CommandLine,ExecutablePath,CreationDate,SessionId FROM Win32_Process";break;
                    case WmiKind.Boot:query="SELECT LastBootUpTime FROM Win32_OperatingSystem";break;
                    default:throw new Blocked("unsupported_metadata");
                }
                object rows=null;
                try {
                    budget.Check();rows=Call(service,"ExecQuery",query,"WQL",48);budget.Check();
                    var result=new NativeWmiRows(rows,kind,budget);rows=null;return result;
                } catch {budget.Capture(QueryPhase(kind));throw;}
                finally {budget.At(FailurePhase.cleanup,delegate {Release(rows);});}
            }
            public void Dispose(){var a=service;var b=locator;service=null;locator=null;ReleaseAll(a,b);}
        }
        sealed class NativeTask : ITask {
            object value; public NativeTask(object task) { value=task; }
            public string Name { get { return (string)Get(value,"Name"); } }
            public string Path { get { return (string)Get(value,"Path"); } }
            public string Xml { get { return (string)Get(value,"Xml"); } }
            public int State { get { return Convert.ToInt32(Get(value,"State"),CultureInfo.InvariantCulture); } }
            public void Dispose() { var old=value;value=null;Release(old); }
        }
        sealed class NativeFolder : IFolder {
            object value;readonly Budget budget;public NativeFolder(object folder,Budget requestBudget) { value=folder;budget=requestBudget; }
            public string Path { get { budget.Check();string path=(string)Get(value,"Path");budget.Check();return path; } }
            public IList<ITask> GetTasks(int flags) {
                var result=new List<ITask>();object collection=null;
                try {
                    budget.Check();collection=Call(value,"GetTasks",1);budget.Check();
                    int count=Convert.ToInt32(Get(collection,"Count"),CultureInfo.InvariantCulture);budget.Check();
                    if(count<0 || count>4096) Unsupported();
                    for(int i=1;i<=count;i++) {budget.Check();result.Add(new NativeTask(Item(collection,i)));budget.Check();}
                    var completed=collection;collection=null;budget.At(FailurePhase.cleanup,delegate {Release(completed);});budget.Check();return result;
                } catch {budget.Capture(FailurePhase.task_read);budget.At(FailurePhase.cleanup,delegate {DisposeAll(result);});throw;}
                finally {budget.At(FailurePhase.cleanup,delegate {Release(collection);});}
            }
            public IList<IFolder> GetFolders(int flags) {
                var result=new List<IFolder>();object collection=null;
                try {
                    budget.Check();collection=Call(value,"GetFolders",0);budget.Check();
                    int count=Convert.ToInt32(Get(collection,"Count"),CultureInfo.InvariantCulture);budget.Check();
                    if(count<0 || count>4096) Unsupported();
                    for(int i=1;i<=count;i++) {budget.Check();result.Add(new NativeFolder(Item(collection,i),budget));budget.Check();}
                    var completed=collection;collection=null;budget.At(FailurePhase.cleanup,delegate {Release(completed);});budget.Check();return result;
                } catch {budget.Capture(FailurePhase.task_read);budget.At(FailurePhase.cleanup,delegate {DisposeAll(result);});throw;}
                finally {budget.At(FailurePhase.cleanup,delegate {Release(collection);});}
            }
            public void Dispose() { var old=value;value=null;Release(old); }
        }
        public static Measurement Collect() {
            var budget=new Budget(2000);object service=null;
            var result=new Measurement { Status="blocked",Reason="query_failed" };
            bool complete=false;
            try {
                if(Environment.OSVersion.Platform!=PlatformID.Win32NT) throw new Blocked("native_windows_required");
                budget.At(FailurePhase.task_service,delegate {var type=Type.GetTypeFromProgID("Schedule.Service",true);service=Activator.CreateInstance(type);});
                budget.At(FailurePhase.task_connect,delegate {Call(service,"Connect");budget.Check();});
                var records=ReadAll(new NativeFolder(budget.At(FailurePhase.task_read,delegate {return Call(service,"GetFolder","\\");}),budget),budget);
                int actions=0;foreach(var record in records) {budget.Check();actions+=record.Actions.Count;}
                result.TaskCount=records.Count;result.ActionCount=actions;complete=true;
            } catch(Blocked error) {budget.Capture(FailurePhase.unknown);result.Reason=error.Reason;}
            catch {budget.Capture(FailurePhase.unknown);result.Reason="query_failed";}
            finally {try {budget.At(FailurePhase.cleanup,delegate {Release(service);});}catch {complete=false;result.Reason="query_failed";} }
            if(complete) {try {budget.At(FailurePhase.task_read,delegate {budget.Check();});result.Status="measured";result.Reason="none";}catch(Blocked error){result.Reason=error.Reason;} }
            if(result.Status!="measured") {result.TaskCount=null;result.ActionCount=null;}
            result.ElapsedMilliseconds=budget.Elapsed;
            if(result.Status=="measured" && result.ElapsedMilliseconds>=2000) {
                budget.Capture(FailurePhase.task_read);result.Status="blocked";result.Reason="deadline_exceeded";result.TaskCount=null;result.ActionCount=null;
            }
            result.FailurePhase=budget.FailurePhase;
            return result;
        }
        public static BasicMeasurement CollectBasic() {
            var budget=new Budget(2000);object service=null;IWmiReader reader=null;bool complete=false;
            var result=new BasicMeasurement {Status="blocked",Reason="query_failed"};
            try {
                if(Environment.OSVersion.Platform!=PlatformID.Win32NT) throw new Blocked("native_windows_required");
                reader=new NativeWmiReader(budget);
                budget.At(FailurePhase.task_service,delegate {budget.Check();service=Activator.CreateInstance(Type.GetTypeFromProgID("Schedule.Service",true));budget.Check();});
                budget.At(FailurePhase.task_connect,delegate {Call(service,"Connect");budget.Check();});
                var records=ReadBasic(reader,new NativeFolder(budget.At(FailurePhase.task_read,delegate {return Call(service,"GetFolder","\\");}),budget),budget);
                result.ServiceCount=records.Services.Count;result.TaskCount=records.Tasks.Count;
                int actions=0;foreach(var task in records.Tasks){budget.Check();actions+=task.Actions.Count;}
                result.ActionCount=actions;result.ProcessCount=records.Processes.Count;result.BootCount=1;complete=true;
            }catch(Blocked error){budget.Capture(FailurePhase.unknown);result.Reason=error.Reason;}catch {budget.Capture(FailurePhase.unknown);result.Reason="query_failed";}
            finally {try {budget.At(FailurePhase.cleanup,delegate {try {if(reader!=null)reader.Dispose();}finally {Release(service);}});}catch {complete=false;result.Reason="query_failed";} }
            if(complete){try {budget.At(FailurePhase.unknown,delegate {budget.Check();});result.Status="measured";result.Reason="none";}catch(Blocked error){result.Reason=error.Reason;} }
            result.ElapsedMilliseconds=budget.Elapsed;
            if(result.Status=="measured" && result.ElapsedMilliseconds>=2000){budget.Capture(FailurePhase.unknown);result.Status="blocked";result.Reason="deadline_exceeded";}
            if(result.Status!="measured"){result.ServiceCount=null;result.TaskCount=null;result.ActionCount=null;result.ProcessCount=null;result.BootCount=null;}
            result.FailurePhase=budget.FailurePhase;return result;
        }
    }
}
