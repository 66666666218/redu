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
