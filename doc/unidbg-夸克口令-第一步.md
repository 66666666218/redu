# unidbg 跑夸克聚安全 —— 第一步(能加载 + 真实执行)

> 2026-10-09 建。目标:**把签名从模拟器里解放出来**。这是唯一能彻底不依赖设备的路。
> 兄弟文档:`scripts/probe_quark_kouling.py` 文末(签名链、oracle、两形态定案)。

---

## 0. 三句话结论

1. **前提已验证成立**:签名是 APK 内置密钥的**纯函数**,与设备/安装/运行时状态无关(做过控制变量)。
2. **第一步达标**:**arm64 的 `libsgmainso-6.6.230703.so` 在纯 PC(Win + unidbg)上加载成功并真实执行**,
   `JNI_OnLoad` 跑进去了。
3. **卡点是"补环境"**(社区术语):SG 特有的几项 JNI 回调要自己实现,第一项是
   `SecurityGuardMainPlugin.getMainPluginClassLoader()`。标准 JNI(`FindClass`/`NewGlobalRef`/`GetMethodID`)
   unidbg 已经兜住了。

---

## 1. 装什么、怎么装(可复跑)

| 件 | 版本/来源 | 备注 |
|---|---|---|
| JDK | Temurin 21(`api.adoptium.net/v3/binary/latest/21/ga/windows/x64/jdk/...`) | **免安装 zip**,解到 `data/_jdk/` |
| unidbg | `zhkl0228/unidbg` **tag `v0.9.8`**(社区一致要求,别用最新) | 解到 `data/_unidbg/unidbg-0.9.8` |
| Maven | 用仓库自带 **`./mvnw.cmd`**(wrapper 会自己下 Maven 3.5.4) | 不需要另装 |
| 后端 | `unidbg-dynarmic` / `unidbg-unicorn2`(Maven Central,**test 作用域**) | Windows 的 dll 在 `backend/*/src/main/resources/natives/windows_64/` |

**★ 必须换阿里云镜像**,否则从 Maven Central 拉依赖是 **1–6 kB/s**(实测):
写 `~/.m2/settings.xml`,`<mirror><url>https://maven.aliyun.com/repository/public</url><mirrorOf>*</mirrorOf></mirror>`
⇒ **1.4 MB/s**,整个 `test-compile` **28 秒**完成。

构建与运行:
```bash
export JAVA_HOME='D:\code\redian\data\_jdk\jdk-21.0.12.1+1'
cd data/_unidbg/unidbg-0.9.8
./mvnw.cmd -B -pl unidbg-android -am -DskipTests test-compile
# 测试类:unidbg-android/src/test/java/com/redian/QuarkSg.java(用 javac 单独编,见 §3 的坑)
java -cp "data/_jars/*;unidbg-android/target/classes;unidbg-api/target/classes;\
unidbg-android/target/test-classes" \
  -Djava.library.path=".../backend/dynarmic/src/main/resources/natives/windows_64" \
  com.redian.QuarkSg data/_sgcase
```

**料**(从 APK 里取,放 `data/_sgcase/`):`libsgmainso` / `libsgmiddletierso` / `libsgsecuritybodyso`
(都改成短名)+ `libc++_shared.so` + `yw_1222.jpg` / `yw_1222_mwua.jpg` + 整个 `quark.apk`。

---

## 2. 实测结果(2026-10-09)

```
[OK-1] loaded base=0x40000000 size=0x200000 cost=875ms
    [Arm64Svc 0x000770] [c12d00d4] 0xfffe0770: "svc #0x16e"    ← 真实 ARM64 指令在执行
LR=RX@0x40074234[libmain.so]0x74234                            ← SG 自己的内部模块
JNIEnv->FindClass(java/lang/Boolean|Integer|String|Long|Float|Double)
JNIEnv->FindClass(com/alibaba/wireless/security/open/SecException)
JNIEnv->NewGlobalRef(...) / GetMethodID(...)                   ← 标准 JNI 全过
java.lang.UnsupportedOperationException:
    com/alibaba/wireless/security/mainplugin/SecurityGuardMainPlugin->getMainPluginClassLoader()
→ Illegal JNI version: 0xffffffff
```

**两条反直觉但重要的观察**:
- **段表被人为打坏**(python 的 ELF 解析器直接抛异常)**挡不住 unidbg** —— 它按段映射,不依赖段表。
- **arm64 独占不是死穴** —— 至少加载与引导阶段完全正常;社区"没有 64 位先例"可能只是没人试。

---

## 3. 踩过的坑(**会再踩,先记住**)

1. **★ classpath 里的中文路径会让 Java 找不到 jar。** jar 都在 `C:\Users\周万鹏\.m2\…`,
   而 Java 在 Windows 上用 `sun.jnu.encoding`(GBK)解析路径、shell 传的是 UTF-8 ⇒ 对不上。
   症状极具迷惑性:**`NoClassDefFoundError` 说某个类找不到,而那个类明明在 cp 里**。
   绕法:把所有 jar **拷到无中文的目录** + 用 `dir/*` 通配。
2. `-DskipTests` 下 `test-compile` **没有编译测试源码**(reactor 里只编了 main)⇒ 直接用 `javac` 单编那个类。
3. `emulator.close()` 抛 `IOException` ⇒ `main` 要 `throws Exception`。
4. `AbstractJni.callStaticObjectMethod` 的第一个参数是 **`BaseVM`** 不是 `VM`(写错会报"没有合适的覆写方法")。
5. Windows 下 `cmd //c mvnw.cmd` 找不到;要 **`./mvnw.cmd`**(Git Bash 直接执行 .cmd)。

---

## 4. 下一步(要继续的话)

按 `sgInnora` 的命令表一项项喂:初始化(`10101` …)→ 中间层(`70101`/`70102`)→
**签名(`10401` = `ISecureSignatureComponent`)**;同族最近参照是**天猫**那份
`22301 → 22302 → 70102`(见记忆 `securityguard-unidbg-blueprints`)。

★ **我们独有的对账手段**:每跑出一步,都拿**已有的 (内容 → 真签名) 样本对**比对
(见 `scripts/probe_quark_kouling.py`),**不用等服务端试错** —— 社区卡在
`701029904`/`701029906` 的人没有这个条件。


---

## 5. 第二步:补上 classloader 与文件层(2026-10-10)

**加的四样**(全部有出处,照天猫那份案例改):

| 加的东西 | 内容 |
|---|---|
| `getMainPluginClassLoader` | 返回一个 ClassLoader 代理 |
| `ClassLoader.loadClass(name)` | 用 unidbg 代理类兜住,让 native 能 `RegisterNatives` |
| 6 个 JNI 覆写 | `callIntMethod`/`callStaticIntMethod`/`callStaticVoidMethod`/`getStaticIntField`/`getStaticLongField`/`setStaticLongField`,给死值 |
| **IOResolver** | `/proc/<pid>/stat`、`/proc/<pid>/wchan`、`/proc/self/status`、**`/proc/cpuinfo`(必须写成 AArch64)**;`app_SGLib/**` 摊**真实文件**(含三份插件清单 `pkgInfo`) |

### 结果

- ✅ `getMainPluginClassLoader` 被吃掉后,**native 不再抛异常,继续跑了很远**;
- ✅ **三个插件库全部加载**:`main` 的 `JNI_OnLoad` 跑完;`securitybody` 的也跑了很远
  (日志里能看到它在 `FindClass`/`GetMethodID`:`SecException`/`Long`/`Float`/`Double`/`ApmMonitorAdapter`);
- ✅ **需要手写的补环境只有 1 项** —— 其余 JNI 需求 **unidbg 默认实现全兜住了**(比天猫那份经验还浅);
- ✅ native **确实去读了文件**(日志里有 `read path=…RandomAccessFile`),说明 IOResolver 生效;
- ⛔ `securitybody` 的 `JNI_OnLoad` **返回 -1** ⇒ `Illegal JNI version: 0xffffffff`。

### 卡点的诊断与下一步

**原因指向顺序**:天猫案例是 `main.JNI_OnLoad → **10101 初始化** → 再加载其余插件`;**我们跳过了 10101**。
障碍:夸克没有 `JNICLibrary`,要先**找出 main 插件注册进来的 native 方法名**才能发 10101。
⇒ 下一步:开 unidbg 的 `AndroidModule`(或 hook `RegisterNatives`)把注册的 (类名, 方法名, 签名) 打出来。

### ★ 顺带踩到的两个 unidbg 自身的坑(与 SG 无关,但会要命)

1. **调试器会卡在 stdin 上。** 当 native 调到一个 unidbg 没注册的 JNI 函数时,unidbg 弹
   `SimpleARM64Debugger`,而它的 `loop()` 里有 `new Scanner(System.in)` + `scanner.nextLine()` ——
   **整个程序就那么停住,一行日志都不再打**,看起来像"补环境卡死了"。
   绕法:`yes c | java …`(继续命令是 **`c`**)。⚠️ **别把它误判成 SG 的坑。**
2. (见 §3)classpath 里的中文路径会让 Java 类加载器找不到 jar。


---

## 6. 第三步:整条链跑通(2026-10-10)

**补齐次序后,三步链走完,退出码 0,总耗时约 990ms:**

```
[1] libsgmainso base=0x40000000
[2] libsgmainso.JNI_OnLoad ✓
[3] 10101 初始化 → null
[4] libsgsecuritybodyso.so.JNI_OnLoad ✓     ← 补了 10101 之后才通过(之前一直返回 -1)
[4] libsgmiddletierso.so.JNI_OnLoad ✓
[5] 10102 ×3 注册三个插件完毕
[6] 10401 签名(内容=hello) → null           ← 命令发出去了,但返回 null
[7] 累计补环境 16 项
```

### ★★ 纠正一条错判(重要)

上一版写「夸克没有 `JNICLibrary`、架构与天猫不同」——**错了**。我搜的是 APK 的 dex;
这个类在 dex 里确实没有,但**运行时 native 自己 `RegisterNatives` 到它上面**(日志实证:
`JNIEnv->RegisterNatives(com/taobao/wireless/security/adapter/JNICLibrary, …, 1)`)。
⇒ **入口与天猫完全一致,配方可直接用。** 教训与 `absence-of-evidence-…` 同源:
**静态搜不到 ≠ 运行时不存在。**

### 补环境清单(16 项,全部有名有姓、机械可补)

- `getXxxClassLoader` 系列(统一成"凡返回 ClassLoader 都给")
- `Context.getPackageCodePath / getPackageName / getFilesDir / getCacheDir / getApplicationInfo / getPackageManager`
- `ApplicationInfo.nativeLibraryDir / sourceDir / dataDir / packageName`
- `java/io/File` 的 `getAbsolutePath / getPath / exists / length / isDirectory / getParent(File)`
- `Build.VERSION.SDK_INT`(=23,与 `AndroidResolver(23)` 一致)、`Build.MODEL/BRAND/MANUFACTURER/DEVICE/PRODUCT/FINGERPRINT`
- `ApmMonitorAdapter.*`(阿里自家埋点门面,一律空转)
- `X->getInstance()LX;` 这类静态单例 → **显式兜底**(但**大声记日志**,记成 `SHIM-BLANK`,
  绝不做静默吞掉 —— 免得把"没补"伪装成"补好了")

### 还差什么(只剩这一段)

`10401` 返回 null,是因为 **SG 遍历参数 map 时用了 unidbg 没实现的集合方法**:

```
java/util/HashMap->keySet()Ljava/util/Set;        ← 已补
java/util/Set->toArray()[Ljava/lang/Object;       ← 下一个
java/lang/Integer-><init>(I)V
```

**⇒ 结论**:unidbg 这条路**技术上成立**;剩下的不是"能不能",而是"**还有几个集合方法要补**"。
补完这一段,`10401` 就能出值 ⇒ 直接与 oracle 的已知答案对账(`2ee1…36ab7dd8…`)。


---

## 7. 第四步:JNI 层打通(2026-10-10)

最终状态:**退出码 0,20 项补环境,1401ms,剩余缺项为空(零 JNI 异常)**。

```
[1] libsgmainso base=0x40000000
[2] libsgmainso.JNI_OnLoad ✓
[3] 10101 初始化 → null
[4] libsgsecuritybodyso.so / libsgmiddletierso.so .JNI_OnLoad ✓
[5] 10102 ×3 注册三个插件完毕
[6] 10401 签名 → null          ← ★ 不再是异常,是"正常返回但值为 null"
[7] 累计补环境 20 项
```

### 20 项补环境(全部有名有姓、机械可补)

| 类 | 项 |
|---|---|
| classloader | `getMainPluginClassLoader` / `getPluginClassLoader` / `getClassLoader`(**统一成"凡返回 ClassLoader 都给"**) |
| Context | `getPackageCodePath` / `getPackageName` / `getFilesDir` / `getCacheDir` / `getApplicationInfo` / `getPackageManager` |
| ApplicationInfo | `nativeLibraryDir` / `sourceDir` / `dataDir` / `packageName` |
| java.io.File | `getAbsolutePath` / `getPath` / `exists` / `length` / `isDirectory` / `getParent(File)` |
| Build | `VERSION.SDK_INT`(23,配 `AndroidResolver(23)`)、`MODEL` / `BRAND` / `MANUFACTURER` / `DEVICE` / `PRODUCT` / `FINGERPRINT` |
| gson/集合 | **`java/util/HashMap->keySet()`**、**`java/util/Set->toArray()`**(unidbg 只实现了 `entrySet`/`iterator`) |
| 装箱 | **`newObject()` 里的 `Integer/Boolean/Long-><init>`** |
| 埋点 | `ApmMonitorAdapter.*` 一律空转 |
| 兜底 | `X->getInstance()LX;` 静态单例 → 代理对象(**大声记 `SHIM-BLANK` 日志,绝不静默吞**) |

### ★ 三个"找错地方"的教训(每个都耗了一轮)

1. **装箱类型的 `<init>` 落在 `AbstractJni.newObject()`**,不在任何 `callXxxMethod` —— 我先后在
   `callStaticVoidMethod` / `callVoidMethod` / `callStaticVoidMethodV` 里补,全都没用。
   **看堆栈比猜快**:`at AbstractJni.newObject(AbstractJni.java:753)`。
2. **unidbg 的 `AbstractJni` 默认实现就是"抛 UnsupportedOperation"**,所以覆写要成对 ——
   `String` 变体和 **`VaList` 变体**都要覆写,否则永远过不去。
3. **`Set->toArray()` unidbg 没实现**(只有 `entrySet`/`iterator`)—— SG 是按 `keySet().toArray()` 拿键的。

### 现在的卡点:**不是 JNI,是 SG 的内部状态/参数形状**

`10401` 正常返回 **null**(不再是异常)。可能原因(待查,按可能性排序):

1. **`10101` 返回 null** —— 它本该返回一个成功码/句柄,而我们给它的参数是照天猫抄的
   (`[context, 3, "", app_SGLib, ""]`),夸克 6.6 可能要别的形状;
2. **HashMap 的形状**:真实链路里 `10401` 是**插件的 Java 层**调的,传的是它自己组装的
   `SecurityGuardParamContext`;我们直接调 `doCommandNative(10401, …)` **跳过了插件内部的准备**;
3. **缺前置步骤**:AVMP 相关的 `60401`(SafeToken)/ `70201`(建 AVMP 实例)可能必须先生效,
   签名才有密钥可用 —— 注意夸克 6.6 **没有独立的 libsgavmp**,AVMP 并进了主库/中间层。

**⇒ 结论**:这条路**技术上成立**且 JNI 层已通;剩下的是"SG 内部状态怎么喂" —— 与社区卡
`701029904`/`701029906` 的那类问题是同一族,但**我们有 oracle 可以逐步对账**。


---

## 8. 第五步:一条重要的路被证伪 —— unidbg **不执行 Java 字节码**

**动机**:`10401` 返回 null,怀疑是"我们手工拼的命令参数不对"。于是想改成
**让包里的 Java 框架驱动**(`SecurityGuardManager.getInstance(ctx).getSecureSignatureComp().sign(pctx)`),
理由是这些类**都在 dex 里**,它们会喂真实参数、自己走完插件初始化。

**结果:此路不通,而且原因在 unidbg 的架构里:**

```java
// DvmObject.callJniMethodObject → callJniMethod →
UnidbgPointer fnPtr = objectType.findNativeFunction(emulator, method);   // ★ 只找 native 函数
```

**unidbg 的 `DalvikVM` 只是一个"壳"**:它给类/字段/方法的**元数据**(供 `FindClass`/`GetMethodID`),
**只派发 native 方法**;纯 Java 方法一律落回我们自己实现的 `AbstractJni` 代理。
⇒ **`SecurityGuardManager` 这些 Java 框架类在 unidbg 里永远不会真的执行。**

**⇒ 推论(重要)**:
- 「往上走一层、让插件自己驱动」**在 unidbg 里不可能**;
- 只能在 **native 侧手工发 `10101/10102/10401`**,而 `10401 → null` 的修复**只能靠把参数喂对**;
- 也就是说:**unidbg 的上限就是"我们能把命令参数猜对"**。

### 下一步(具体且可行)

**用 Frida 在真机上 hook `JNICLibrary.doCommandNative`,把真实的 `10101` / `10102` / `10401`
参数原样打出来**,再喂给 unidbg。我们前面已经在同一台设备上跑通过多次 Frida
(hook `com.uc.encrypt.a.f` / `UnetCrypt.signWithNumber`),这条路是现成的。

拿到真参数之后:unidbg 里的 `10401` 才有机会出值 ⇒ 与 oracle 的已知答案对账
(`2ee1…36ab7dd8…`)。


---

## 9. 第六步:从"不看星数"的开源搜索里挖到的东西(2026-10-10)

用户提醒:**别只盯高星仓库**。换了一批**专属 6.6 插件架构的串**去搜
(`middletierplugin` / `SecurityGuardMiddleTierPlugin` / `SGPluginExtras` / `app_SGLib` /
`SG_INNER_DATA` / `libsgmiddletier`),挖到三样**低星但关键**的东西:

| 来源 | 挖到什么 |
|---|---|
| **`WithHades/forest`**(低星) | 一份**完整可跑的 unidbg 聚安全签名**。★ **它的 `10401` 形状和天猫那份完全不同**:`[ArrayObject([内容]), "rpc-sdk-online", 0, ""]` ⇒ **参数形状随 app/版本变** |
| **`Jokky6/tb/lazada.java`** | ★★ **SG 会读调用栈做反篡改**:lazada 专门伪造了一串 `StackTraceElement`(`JDKStack → JNICLibrary.doCommandNative(Native Method) → mainplugin.a.doCommand → middletierplugin…`)。**栈不对 ⇒ 不报错、直接返回 null** —— 与我们的症状形态一致 |
| `sgInnora` | `analysis/version_comparison_v8000_v9000.md` 等版本对比(**尚未细看**) |

### 实测结果

- 伪造调用栈**已按夸克路径加进去**,但 **SG 在我们这条路径上并没有真的调用 `Thread.getStackTrace`**
  (日志里只有 `GetMethodID`,没有调用)⇒ **它不是本次 null 的原因**;
- **10401 两种形状都试了**(HashMap 版 / ArrayObject 版),**都是 null**;
- **零缺项、零异常** ⇒ 卡点不在 JNI 层。

### 判断

**`10101` 返回 null(它本该返回成功码/句柄)**,说明**初始化本身可能没真正生效** ——
这靠"猜参数"猜不出来。**静态推理这条路已经到头。**

⇒ 下一步只有两条:
- **(a) 回真机抓真实参数**(Frida hook `JNICLibrary.doCommandNative`,把真的 10101/10102/10401 打出来)。
  ⚠️ 那是 native 方法,在 Houdini 上替换有崩的风险,且要用**生产模拟器** ⇒ **需用户点头**;
- **(b) 继续在 unidbg 里盲试** —— 成功率低。

### 补记:又扫了一圈(不设星数门槛),把公开玩家摸清了

| 仓库 | 星 | 是什么 | 对我们有用吗 |
|---|---|---|---|
| `wzmwayne/fq-sign-api` | **0** | 番茄小说签名服务(**unidbg + JNI 补环境**),基于 `anjia0532/unidbg-boot-server` 骨架 | 一份完整的 `AbstractJni` 参考;**但覆写清单里没有我们没做过的东西**(VaList 变体、伪造 `StackTraceElement`,我们都独立踩到并做了) |
| `iftoif/hongguo-desktop-mac` | **0** | FastAPI + **unidbg 签名服务**(红果/字节 `libmetasec_ml.so`) | 服务化架构参考;但不是阿里系 |
| `CrackerCat/unidbg-qd-sign` | **0** | unidbg 取签名 | 同类骨架 |
| `CrackerBot/bilibili-sign-reverse` | 2 | Frida + OLLVM 平坦化还原 + unidbg | 方法论参考 |
| `LinXunFeng/fix_confict_SecurityEnvSDK_SGMain` | 3 | SGMain 相关 | 边角 |
| `wqzhellohhwy/libnetcomm-rev-mcp` | 1 | TPRT 加固逆向的 MCP 工具集 | 工具性质 |

**★ 一条硬结论**:拿"伪造调用栈的招牌写法" `JNICLibrary.doCommandNative(Native Method)` 去搜,
**全网只有 1 个仓库命中(`Jokky6/tb/lazada.java`)** ⇒ **这个领域的公开玩家就那几个,已经全部找到**。

**⇒ 公开世界不存在 阿里聚安全 6.6 / 夸克 的 unidbg 案例。** 现有公开案例覆盖的是:
天猫/拉扎达(6.5)、forest(6.4)、支付宝(sgInnora)、libsgmain 反混淆(ylcangel)——
**全部是"旧插件架构之前"或"别的 app"**。


---

## 10. 第七步:真机抓包(用户批准走 (a))—— 拿到真参数,但 unidbg 仍不出值

### 怎么抓的(方法本身值得复用)

`JNICLibrary` **不在 App 自己的 classloader 里**(实测 `ClassNotFoundException: DexPathList[[base.apk]]`)
—— 它由**聚安全自己的 classloader** 在内存里加载(这就是"插件体系"的实质,也解释了为什么 APK dex 里搜不到它)。
**绕法**:遍历所有 classloader 找能 `loadClass` 到它的那个,再用 `Java.ClassFactory.get(loader).use(...)` 挂钩:

```js
Java.enumerateClassLoaders({
  onMatch: function (loader) {
    try { loader.loadClass("com.taobao.wireless.security.adapter.JNICLibrary");
          var J = Java.ClassFactory.get(loader).use("...JNICLibrary");
          J.doCommandNative.overload("int","[Ljava.lang.Object;").implementation = function (code, arr) {...};
    } catch (e) {}
  }, onComplete: function(){}
});
```

⚠️ late attach 下挂 native 方法**没有崩**(墓碑 20→20);上次崩的是 **spawn** 模式。

### 抓到的真值(2026-10-10)

```
10401 → [ {INPUT=QUARK/~498b3bHYcr~:/~498b3bHYcr1791563432556}, "12001", "3", "", "true" ]
                                                                    ↑★★★         ↑
10601 → ['1','16','0','12001','[]','']      ← 签名前大量调用;第 4 参就是密钥号
```

- **★ 第 3 个参数是 `3`,不是天猫 6.5 的 `7`**;第 4 个不是 `null` 而是**空串** ⇒ 已改;
- **✅ 顺带确认了签名内容公式**(与我们的 oracle 完全一致):
  `INPUT = "QUARK" + 口令原文 + identifier + token + timestamp`;
- **✅ 拿到一条新样本**(可用于对账):
  content `QUARK/~498b3bHYcr~:/~498b3bHYcr1791563430294`
  → 聚安全签名 `2ee1e0f9d2bc5ffe66b71093b1cb970e56ec24f2aedf`(前缀 `2ee1` = 密钥号 12001)。

### 结果:**还是 null**

按真机参数改完后,`10101` / `10601` / `10401` **依然全部返回 null**。

**⇒ 这不是调用姿势的问题 —— 是 native 端自己决定返回 null**,即 **SG 的内部状态在 unidbg 里始终建立不起来**。
这正是社区卡 `701029904`/`701029906` 的**同一档**。

### ★ 阶段结论

- **我们走到了公开世界的最前面**:公开案例全部覆盖"6.5 及更早 / 别的 app",**夸克 6.6 没有先例**;
- 但要再往前,**缺的不只是参数,而是"整个初始化序列 + 设备注册"这一整块黑盒状态**;
- ⇒ **性价比已经很低**,建议**冻结这条线**。


---

## 11. ★★★ 第八步:方法论突破 —— **聚安全的插件 dex 就在 `libsg*.so` 里,可以反编译读源码**

### 怎么发现的

早先挂钩 `JNICLibrary` 时,classloader 自己暴露了答案:

```
loader: PathClassLoader[DexPathList[[zip file ".../lib/arm64/**libsgmain.so**"], …]]
```

⇒ **那三个几十 KB 的 `libsg*.so` 其实是 ZIP/APK**,里面装着聚安全插件的 `classes.dex`:

| 文件 | 内含 |
|---|---|
| `libsgmain.so`(39KB) | `classes.dex` 76,608B / **58 个类** |
| `libsgmiddletier.so`(36KB) | `classes.dex` 64,852B / 46 个类 |
| `libsgsecuritybody.so`(41KB) | `classes.dex` 71,340B / 60 个类 |

**⇒ 从此所有参数问题都不必再猜 —— 直接读源码。**

### 读到了什么(这一步的价值)

`libsgmain.so` 的 `classes.dex` 里,命令总线入口是
`Lcom/alibaba/wireless/security/mainplugin/б;.doCommand(I[Ljava/lang/Object;)`;
`10101` / `10401` / `10601` 的调用点分别是:

```
SecurityGuardMainPlugin.onPluginLoaded(Context, IRouterComponent, ISGPluginInfo, String, Object[])  ← 10101
...signRequest(SecurityGuardParamContext, …)                                                        ← 10401
...(I I I String; [B String;)[B                                                                     ← 10601
```

**★ `10101` 的真实参数 —— 和我们猜的完全不同:**

```java
const/16  v15, 10
new-array v15, 10, Object[]
  [0] = context
  [1] = Integer(0)                  ← 真机是 0;天猫/forest 抄的是 3
  [2][3][4] = 来自框架传入的 Object[](可为 "")
  [5] = context.getPackageName()
  [6] = PackageInfo.versionName      ← "7.14.2.872"
  [7] = Build.VERSION.RELEASE        ← "14"
  [8] = ActivityManager 里本进程的 processName
  [9] = Integer(0)
doCommand(10101, v15)
```

**我们之前只传了 5 个参数** ⇒ **这是"所有命令都返回 null"的根因。**

### 修完之后的实测变化

| | 修之前 | 修之后 |
|---|---|---|
| `10101` | **秒回 null** | **真的跑起来了**:依次问 `getPackageCodePath` / `getFilesDir` / `getApplicationInfo` / `nativeLibraryDir` / `ApmMonitorAdapter`(SHIM #2–#6) |
| `10102` | 无反应 | **真的在读我们的参数**:`GetArrayLength(["main","6.6.230703",…]) → 3` → `GetObjectArrayElement(0) → "main"` → `GetStringUtfChars("main")` |
| 结局 | 全 null | **卡在 `10102` 处理 "main" 的过程中**(native 内部,无网络尝试,日志停住) |

### 下一步

卡点从"参数猜不对"变成"**`10102` 内部卡住**"—— 这是**可查**的:
继续用同一套办法(反编译插件 dex)读 `10102` 的处理逻辑,看它卡在哪一步;
必要时看它要读的文件(IOResolver 是否漏了路径)。


---

## 12. 第九步:死循环被逐层挖到「哪一行、哪个系统调用」

### 挖掘方法(自造,可复用)

死循环时日志是**静默的**(零系统调用、零 JNI 报错),所以自造了一个 **热点指令探测器**:
挂 `CodeHook` 统计每个地址的命中次数,某个地址被命中超过阈值就打印出来 —— 一次就定位到循环体。

### 逐层结果

| 层 | 结果 |
|---|---|
| 模块 | **`libc.so`**(base 0x40340000) |
| Offset | `0x17830`(一个 **PLT 跳转桩**,被打转 20 万次) |
| 它跳向哪 | 读 GOT 槽 `libc+0xd8050` = `0x4035f1f8` = **`libc+0x1f1f8` = `__errno()`** |
| 调用者(抓 LR) | **`libc+0x68a58`** |
| 那一行在干什么 | 反汇编:`bl __errno` → `mov x0, #0x62`(**系统调用号 98 = `futex`**)→ `bl syscall` → `cmn w0,#1` → **`b.ne` 失败则读 errno 重试** |

**⇒ 结论:某段 libc 代码在 `futex` 上死循环 —— 调用返回 -1 ⇒ 读 errno ⇒ 立刻重试 ⇒ 无限。**

### unidbg 侧的原因(已定位到具体开关)

```java
// AndroidSyscallHandler.futex()
case FUTEX_WAIT:
    if (old != val) return -EAGAIN;
    if (threadDispatcherEnabled && runningTask != null) { ...真正阻塞...; throw new ThreadContextSwitchException(); }
    else return 0;          // ← 没开调度器 ⇒ 立刻返回 ⇒ 调用方以为"等到了" ⇒ 空转
```

**unidbg 默认 `threadDispatcherEnabled = false`**(全仓搜过:**没有任何地方把它设成 true**)。
已尝试 `handler.setEnableThreadDispatcher(true)`,但**尚未解决** —— 说明还有别的条件(例如 `runningTask == null`,
即"等待"发生在**主任务**里而不是独立线程里,那时分支仍走 `return 0`)。

### 现在的准确位置

**离打通只差"让 `FUTEX_WAIT` 真的挂起"这一步**,而它牵扯 unidbg 的线程调度内部。
下一步要么继续读 unidbg 的 `UniThreadDispatcher`,要么**在 libc 的 `futex` 调用点直接改行为**
(例如让 `libc+0x68a58` 那个循环的返回恒为 0)。
