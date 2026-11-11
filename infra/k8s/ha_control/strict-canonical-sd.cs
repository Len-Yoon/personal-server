using System;
using System.IO;
using System.Collections.Generic;
// Narrow offline equality contract. Does not call any Windows write API.
public static class StrictCanonicalSd {
 static void ValidatePolicy(byte[] raw){
  Type policy=null;foreach(var assembly in AppDomain.CurrentDomain.GetAssemblies()){
   Type found=assembly.GetType("HaNamespacePolicyCoreV1",false,false);
   if(found!=null){Need(policy==null);policy=found;}
  }
  Need(policy!=null);var method=policy.GetMethod("SaclPresent",new[]{typeof(byte[])});
  Need(method!=null&&method.ReturnType==typeof(bool));method.Invoke(null,new object[]{raw});
 }
 static void Need(bool value){if(!value)throw new InvalidOperationException("canonical_descriptor_rejected");}
 static ushort U16(byte[] b,int o){return (ushort)(b[o]|b[o+1]<<8);}
 static uint U32(byte[] b,int o){return (uint)(b[o]|b[o+1]<<8|b[o+2]<<16|b[o+3]<<24);}
 static void W16(byte[] b,int o,int n){b[o]=(byte)n;b[o+1]=(byte)(n>>8);}
 static void W32(byte[] b,int o,int n){for(int i=0;i<4;i++)b[o+i]=(byte)(n>>(8*i));}
 static int SidLength(byte[] b,int o,int limit){Need(o>=0&&o<=limit-8&&b[o]==1&&b[o+1]<=15);int n=8+4*b[o+1];Need(o<=limit-n);return n;}
 static byte[] Copy(byte[] b,int o,int n){var r=new byte[n];Buffer.BlockCopy(b,o,r,0,n);return r;}
 static byte[] CompactAcl(byte[] b,int o,int n,bool dacl){
  int at=o+8,count=U16(b,o+4);Need(count<=128);int limit=o+n;
  for(int i=0;i<count;i++){
   Need(at<=limit-8);int size=U16(b,at+2);Need(size>=16&&(size&3)==0&&at<=limit-size);
   // The existing policy accepts simple allow/deny DACL ACEs. This utility
   // additionally restricts SACLs to understood, simple system-audit ACEs.
   Need(dacl?(b[at]==0||b[at]==1):b[at]==2);
   Need(size==8+SidLength(b,at+8,at+size));at+=size;
  }
  Need(((limit-at)&3)==0);for(int i=at;i<limit;i++)Need(b[i]==0);
  byte[] result=Copy(b,o,at-o);W16(result,2,result.Length);return result;
 }
 public static byte[] Canonicalize(byte[] raw){
  // Invoke the unchanged production policy parser first, including its full
  // descriptor domain, SID, DACL, ACL and control checks. No Summary comparison.
  ValidatePolicy(raw);
  bool[] used=new bool[raw.Length];for(int i=0;i<20;i++)used[i]=true;
  byte[][] parts=new byte[4][];
  for(int k=0;k<4;k++){
   uint u=U32(raw,4+4*k);if(u==0)continue;Need(u<=65536);int off=(int)u;Need(off>=20&&(off&3)==0&&off<raw.Length);
   int n=k<2?SidLength(raw,off,raw.Length):U16(raw,off+2);Need(n>=8&&off<=raw.Length-n);
   // Reject all overlapping components, including exact aliasing; ambiguity is
   // outside this deliberately narrower canonical contract.
   for(int i=off;i<off+n;i++){Need(!used[i]);used[i]=true;}
   parts[k]=k<2?Copy(raw,off,n):CompactAcl(raw,off,n,k==3);
  }
  for(int i=20;i<raw.Length;i++)if(!used[i])Need(raw[i]==0);
  using(var stream=new MemoryStream()){
   stream.Write(raw,0,20);int[] offsets=new int[4];
   for(int k=0;k<4;k++)if(parts[k]!=null){while((stream.Length&3)!=0)stream.WriteByte(0);offsets[k]=(int)stream.Length;stream.Write(parts[k],0,parts[k].Length);}
   byte[] result=stream.ToArray();for(int k=0;k<4;k++)W32(result,4+4*k,offsets[k]);
   // Revision, reserved byte and ALL control bits remain byte-for-byte intact.
   ValidatePolicy(result);return result;
  }
 }
 public static bool Equal(byte[] a,byte[] b){a=Canonicalize(a);b=Canonicalize(b);if(a.Length!=b.Length)return false;int diff=0;for(int i=0;i<a.Length;i++)diff|=a[i]^b[i];return diff==0;}
}
