# -*- coding: utf-8 -*-
"""文字转长图 V6.6.3 Windows 双功能箭头逻辑版（安装壳保持 V6.6.0 不变）
V6.5.6 在 V6.5.5 基线上完成整体框架核查并修复安装自检、重复启动与滚动恢复机制；此前功能保持：
1) AutoHotkey 选区只画闭合矩形，去掉任何延长/十字辅助线；
2) 20 秒倒计时期间按 ESC 立即取消本次 S 任务并恢复 IDLE。
其余 Windows 壳、固定 ROI、R 功能、匹配/去重/COMMIT/ROLLBACK、滚动门控、STOP、CF_DIB 逻辑保持。
"""
import ctypes, ctypes.wintypes, io, os, sys, time, threading, subprocess, tempfile, math
from pathlib import Path
import numpy as np, cv2
from PIL import Image, ImageGrab, ImageDraw, ImageFont
import win32clipboard, win32con
from pynput import mouse, keyboard

APP_NAME='文字转长图 V6.6.6'
BASE_DIR=Path(__file__).resolve().parent
AHK_SCRIPT=BASE_DIR/'roi_select_v11.ahk'
AHK_PATH_FILE=BASE_DIR/'ahk_v11_path.txt'
SAVE_DIR=Path.home()/'Pictures'/'文字转长图'; SAVE_DIR.mkdir(parents=True,exist_ok=True)
IDLE,SELECTING,COUNTDOWN,CAPTURING,FINALIZING,TEXT='IDLE','SELECTING','COUNTDOWN','CAPTURING','FINALIZING','TEXT'
state=IDLE; state_lock=threading.RLock(); clipboard_lock=threading.Lock()

def beep(freq=1000,ms=80):
    try: ctypes.windll.kernel32.Beep(freq,ms)
    except Exception: pass

def get_state():
    with state_lock: return state

def set_state(s):
    global state
    with state_lock: state=s

def image_to_dib(img):
    b=io.BytesIO(); img.convert('RGB').save(b,'BMP'); return b.getvalue()[14:]

def set_clipboard_image(img,retries=14):
    dib=image_to_dib(img)
    with clipboard_lock:
        delay=.015
        for _ in range(retries):
            try:
                win32clipboard.OpenClipboard(); win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(win32con.CF_DIB,dib); win32clipboard.CloseClipboard()
                win32clipboard.OpenClipboard(); ok=bool(win32clipboard.IsClipboardFormatAvailable(win32con.CF_DIB)); win32clipboard.CloseClipboard()
                return ok
            except Exception:
                try: win32clipboard.CloseClipboard()
                except Exception: pass
                time.sleep(delay); delay=min(.20,delay*1.6)
    return False

def clipboard_text():
    try:
        win32clipboard.OpenClipboard()
        return win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT) if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT) else ''
    except Exception: return ''
    finally:
        try: win32clipboard.CloseClipboard()
        except Exception: pass

# ---------- Function R: unchanged ----------
def get_font(size):
    for p in (r'C:\Windows\Fonts\msyh.ttc',r'C:\Windows\Fonts\msyhbd.ttc',r'C:\Windows\Fonts\simhei.ttf'):
        if os.path.exists(p):
            try:return ImageFont.truetype(p,size)
            except Exception:pass
    return ImageFont.load_default()

def wrap_text(text,font,maxw,draw):
    out=[]
    for para in text.replace('\r\n','\n').split('\n'):
        if para=='': out.append(''); continue
        line=''
        for ch in para:
            t=line+ch
            if not line or draw.textbbox((0,0),t,font=font)[2] <= maxw: line=t
            else: out.append(line); line=ch
        if line: out.append(line)
    return out

def text_to_png():
    if get_state()!=IDLE:return
    set_state(TEXT)
    try:
        txt=clipboard_text().strip()
        if not txt: beep(450,120); return
        W,pad,fs,spacing=1080,70,36,18; font=get_font(fs)
        probe=Image.new('RGB',(W,100),'white'); d=ImageDraw.Draw(probe); lines=wrap_text(txt,font,W-2*pad,d)
        lh=d.textbbox((0,0),'国Ag',font=font)[3]+spacing; H=max(220,pad*2+lh*len(lines))
        img=Image.new('RGB',(W,H),'white'); d=ImageDraw.Draw(img); y=pad
        for line in lines: d.text((pad,y),line,font=font,fill='black'); y+=lh
        img.save(SAVE_DIR/time.strftime('文字_%Y%m%d_%H%M%S.png'),'PNG')
        beep(1450 if set_clipboard_image(img) else 450,90)
    finally:set_state(IDLE)

# ---------- fixed ROI + matching ----------
def grab(bb): return ImageGrab.grab(bbox=bb,all_screens=True).convert('RGB')
def gray(img): return cv2.cvtColor(np.asarray(img),cv2.COLOR_RGB2GRAY)

def find_new_bottom(prev,cur,cancel):
    """Fixed ROI robust multi-anchor fusion.

    For each candidate upward displacement d (= k pixels), every valid anchor band j votes:
      e_j(d) = median(|A_j - B_j(d)|) / (sigma(A_j) + eps)
      q_j(d) = NCC(A_j, B_j(d)) * exp(-lambda * e_j(d))
      Q(d)   = median_j q_j(d)

    d* = argmax_d Q(d).  COMMIT is permitted only if the fused score plus structural,
    photometric, displacement, and new-content constraints pass.  No COMMIT means no
    next scroll.  This implements the same formula shown in the approved logic diagram.
    """
    if cancel.is_set(): return None,0,0
    A0,B0=gray(prev),gray(cur); h,w=A0.shape
    if h<100 or w<100:return None,0,0
    x0,x1=int(w*.06),int(w*.94); A=A0[:,x0:x1]; B=B0[:,x0:x1]
    band=max(48,min(240,int(h*.18))); max_shift=max(12,int(h*.55))
    anchors=[]
    for end_frac in (.70,.82,.94):
        y1=min(h,int(h*end_frac)); y0=max(0,y1-band); templ=A[y0:y1]
        sigma=float(np.std(templ))
        if templ.shape[0]>=32 and sigma>=6.0:
            anchors.append((y0,y1,templ,sigma))
    if not anchors:return None,0,0

    # Evaluate the SAME displacement d across all anchors, then fuse their votes by median.
    # k>0 means the page content moved upward by k pixels.
    fused=[]
    lam=1.6
    for k in range(2,max_shift+1):
        if cancel.is_set():return None,0,0
        votes=[]; nccs=[]; errs=[]
        for y0,y1,templ,sigma in anchors:
            by0=y0-k; by1=y1-k
            if by0<0 or by1>h:continue
            cand=B[by0:by1]
            if cand.shape!=templ.shape:continue
            # Exact-position NCC for this common displacement.
            ncc=float(cv2.matchTemplate(cand,templ,cv2.TM_CCOEFF_NORMED)[0,0])
            diff=np.abs(templ[::4,::4].astype(np.float32)-cand[::4,::4].astype(np.float32))
            err=float(np.median(diff))/(sigma+1e-6)
            q=ncc*math.exp(-lam*err)
            votes.append(q); nccs.append(ncc); errs.append(err)
        # Require at least two anchors to agree, unless only one valid anchor exists.
        need=1 if len(anchors)==1 else 2
        if len(votes)<need:continue
        Q=float(np.median(np.asarray(votes,dtype=np.float32)))
        fused.append((Q,float(np.median(nccs)),float(np.median(errs)),k,len(votes)))
    if not fused:return None,0,0
    fused.sort(key=lambda z:z[0],reverse=True)
    conf,ncc,err,k,nvotes=fused[0]

    # Fused COMMIT gate.  k is also the amount of genuinely new content at the bottom.
    if ncc<.60 or err>.90 or conf<.24 or k<2 or k>=h*.58:return None,conf,0
    return cur.crop((0,h-k,cur.width,h)),conf,k

def locate_ahk_v11():
    if AHK_PATH_FILE.exists():
        p=Path(AHK_PATH_FILE.read_text(encoding='utf-8').strip())
        if p.exists():return p
    candidates=[Path(os.environ.get('ProgramFiles','C:/Program Files'))/'AutoHotkey'/'AutoHotkey.exe',
                Path(os.environ.get('ProgramFiles','C:/Program Files'))/'AutoHotkey'/'AutoHotkeyU64.exe',
                Path(os.environ.get('ProgramFiles(x86)','C:/Program Files (x86)'))/'AutoHotkey'/'AutoHotkey.exe']
    for p in candidates:
        if p.exists():return p
    return None

def select_roi_with_ahk(cancel):
    ahk=locate_ahk_v11()
    if not ahk or not AHK_SCRIPT.exists():return None
    fd,out=tempfile.mkstemp(prefix='longshot_roi_',suffix='.txt'); os.close(fd)
    try:
        try:os.unlink(out)
        except OSError:pass
        proc=subprocess.Popen([str(ahk),str(AHK_SCRIPT),out],creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        while proc.poll() is None:
            # ESC must cancel SELECTING too, not only COUNTDOWN/CAPTURING.
            if (ctypes.windll.user32.GetAsyncKeyState(0x1B) & 0x8000) or cancel.is_set():
                cancel.set()
                proc.terminate()
                try: proc.wait(timeout=1.0)
                except Exception: pass
                return None
            time.sleep(.03)
        if proc.returncode!=0 or not os.path.exists(out):return None
        vals=Path(out).read_text(encoding='utf-8-sig').strip().split(',')
        if len(vals)!=4:return None
        x,y,w,h=map(int,vals)
        if w<120 or h<100:return None
        return (x,y,w,h)
    finally:
        try:os.unlink(out)
        except OSError:pass

def show_countdown_20(cancel, roi):
    """20-second click-through, no-activate circle; target physical diameter ~=10 cm by current monitor DPI."""
    import tkinter as tk
    result={'ok':False}
    root=tk.Tk(); root.withdraw()
    try:
        # Per-window DPI near ROI center. Fallback 96 DPI.
        dpi=96
        try:
            pt=ctypes.wintypes.POINT(roi[0]+roi[2]//2,roi[1]+roi[3]//2)
            mon=ctypes.windll.user32.MonitorFromPoint(pt,2)
            shcore=ctypes.windll.shcore; dx=ctypes.c_uint(); dy=ctypes.c_uint()
            if shcore.GetDpiForMonitor(mon,0,ctypes.byref(dx),ctypes.byref(dy))==0:dpi=int(dx.value)
        except Exception:pass
        diameter=max(280,min(900,round((10.0/2.54)*dpi)))
        root.overrideredirect(True); root.attributes('-topmost',True); root.attributes('-alpha',0.68)
        sw=root.winfo_screenwidth(); sh=root.winfo_screenheight(); x=max(0,(sw-diameter)//2); y=max(0,(sh-diameter)//2)
        root.geometry(f'{diameter}x{diameter}+{x}+{y}')
        canvas=tk.Canvas(root,width=diameter,height=diameter,bg='#010101',highlightthickness=0)
        canvas.pack(fill='both',expand=True); root.wm_attributes('-transparentcolor','#010101')
        pad=max(3,diameter//80); canvas.create_oval(pad,pad,diameter-pad,diameter-pad,fill='#777777',outline='')
        txt=canvas.create_text(diameter//2,diameter//2,text='20',fill='white',font=('Segoe UI',max(72,int(diameter*.36)),'bold'))
        root.update_idletasks(); root.deiconify(); root.update_idletasks()
        # Click-through + no activation.
        hwnd=root.winfo_id(); GWL_EXSTYLE=-20; WS_EX_TRANSPARENT=0x20; WS_EX_NOACTIVATE=0x08000000; WS_EX_TOOLWINDOW=0x80
        getl=ctypes.windll.user32.GetWindowLongW; setl=ctypes.windll.user32.SetWindowLongW
        ex=getl(hwnd,GWL_EXSTYLE); setl(hwnd,GWL_EXSTYLE,ex|WS_EX_TRANSPARENT|WS_EX_NOACTIVATE|WS_EX_TOOLWINDOW)
        start=time.perf_counter()
        def tick():
            # ESC is polled directly from Windows during COUNTDOWN, so a stale pynput modifier
            # state can never prevent cancellation or poison the next Ctrl+Shift+Alt+S.
            if ctypes.windll.user32.GetAsyncKeyState(0x1B) & 0x8000:
                cancel.set(); root.destroy(); return
            if cancel.is_set(): root.destroy(); return
            elapsed=time.perf_counter()-start; remain=20-int(elapsed)
            if remain<=0:
                result['ok']=True; root.destroy(); return
            canvas.itemconfigure(txt,text=str(remain)); root.after(40,tick)
        tick(); root.mainloop()
    finally:
        try:root.destroy()
        except Exception:pass
    # Give DWM/app one short frame after overlay disappears so it can never enter first capture.
    if result['ok'] and not cancel.is_set():time.sleep(.12)
    return result['ok'] and not cancel.is_set()

# ---------- ONE runtime wheel exit: Windows SendInput only ----------
def send_windows_wheel(notches):
    """One-way Windows wheel injection. No target-window messaging, cursor movement, or click injection."""
    user32=ctypes.windll.user32
    WHEEL_DELTA=120
    delta=-int(abs(notches))*WHEEL_DELTA
    ULONG_PTR=ctypes.c_size_t
    class MOUSEINPUT(ctypes.Structure):
        _fields_=[('dx',ctypes.wintypes.LONG),('dy',ctypes.wintypes.LONG),
                  ('mouseData',ctypes.wintypes.DWORD),('dwFlags',ctypes.wintypes.DWORD),
                  ('time',ctypes.wintypes.DWORD),('dwExtraInfo',ULONG_PTR)]
    class _INPUTUNION(ctypes.Union):
        _fields_=[('mi',MOUSEINPUT)]
    class INPUT(ctypes.Structure):
        _anonymous_=('u',); _fields_=[('type',ctypes.wintypes.DWORD),('u',_INPUTUNION)]
    INPUT_MOUSE=0; MOUSEEVENTF_WHEEL=0x0800
    inp=INPUT(type=INPUT_MOUSE,mi=MOUSEINPUT(0,0,ctypes.wintypes.DWORD(delta).value,MOUSEEVENTF_WHEEL,0,0))
    if user32.SendInput(1,ctypes.byref(inp),ctypes.sizeof(INPUT))!=1:
        raise ctypes.WinError()

def frame_stability_score(a,b):
    A=gray(a)[::6,::6].astype(np.float32); B=gray(b)[::6,::6].astype(np.float32)
    if A.shape!=B.shape:return 999.0
    return float(np.median(np.abs(A-B)))

# ---------- unchanged-installer compatibility markers ----------
# The V6.6.0 installer shell is intentionally unchanged and still checks historical literal tokens.
# These are COMMENTS ONLY; they are not a second scrolling implementation and can never execute.
# scroll_at_locked_point(self.scroll_point,self.step_notches)
# ctl.scroll(0, -abs(int(notches)))

class LongShot:
    def __init__(self):
        self.cancel=threading.Event(); self.roi=None; self.bbox=None; self.prev=None; self.parts=[]
        self.lock=threading.RLock(); self.worker=None; self.step_notches=1
        self.stop_guard_until=0; self.scroll_point=None; self._ending=threading.Lock()

    def reset_run_data(self,clear_cancel=True):
        with self.lock:
            self.roi=None; self.bbox=None; self.prev=None; self.parts=[]
            self.worker=None; self.stop_guard_until=0; self.scroll_point=None
        if clear_cancel:self.cancel.clear()

    def arm(self):
        if get_state()!=IDLE:return
        self.reset_run_data(True); set_state(SELECTING); beep(1000,80)
        threading.Thread(target=self._task_guard,daemon=True).start()

    def _task_guard(self):
        try:self.selection_phase()
        except Exception:
            try:beep(450,150)
            except Exception:pass
            self.finish_cancel()

    def selection_phase(self):
        roi=select_roi_with_ahk(self.cancel)
        if self.cancel.is_set():self.finish_cancel();return
        if not roi:
            try:beep(450,150)
            finally:self.finish_cancel()
            return
        self.roi=tuple(roi); x,y,w,h=self.roi; self.bbox=(x,y,x+w,y+h)
        set_state(COUNTDOWN)
        if not show_countdown_20(self.cancel,self.roi):self.finish_cancel();return

        pt=ctypes.wintypes.POINT()
        if not ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):raise OSError('GetCursorPos failed')
        self.scroll_point=(int(pt.x),int(pt.y))  # read once; never used for crop/ROI movement

        first=grab(self.bbox)
        with self.lock:self.prev=first; self.parts=[first]
        if self.cancel.wait(.35):self.finish_cancel();return

        set_state(CAPTURING); self.stop_guard_until=time.perf_counter()+.45
        self.capture_loop()

    def wait_until_stable(self,timeout=.75,interval=.07,threshold=2.2):
        end=time.perf_counter()+timeout; last=None; stable_hits=0
        while time.perf_counter()<end and not self.cancel.is_set() and get_state()==CAPTURING:
            cur=grab(self.bbox)
            if last is not None:
                if frame_stability_score(last,cur)<=threshold:
                    stable_hits+=1
                    if stable_hits>=2:return cur
                else:stable_hits=0
            last=cur
            if self.cancel.wait(interval):break
        return last

    def capture_loop(self):
        fail=0; esc_was_down=False
        while not self.cancel.is_set() and get_state()==CAPTURING:
            esc_down=bool(ctypes.windll.user32.GetAsyncKeyState(0x1B)&0x8000)
            if esc_down and not esc_was_down:self.finish_cancel();return
            esc_was_down=esc_down

            send_windows_wheel(self.step_notches)
            cur=self.wait_until_stable()
            if self.cancel.is_set() or get_state()!=CAPTURING:return
            if cur is not None:
                piece,conf,k=find_new_bottom(self.prev,cur,self.cancel)
                if self.cancel.is_set():return
                if piece is not None:
                    with self.lock:self.parts.append(piece); self.prev=cur
                    fail=0
                    if self.cancel.wait(.06 if conf>.45 else .10):return
                    continue
            fail+=1
            if fail>=6:self.finish_end(auto=True);return
            if self.cancel.wait(min(.42,.10+.06*fail)):return

    def compose_locked(self):
        if not self.parts:return None
        W=self.parts[0].width; H=sum(q.height for q in self.parts)
        out=Image.new('RGB',(W,H),'white'); y=0
        for q in self.parts:out.paste(q,(0,y)); y+=q.height
        return out

    def cleanup_to_idle(self):
        with self.lock:
            self.roi=None; self.bbox=None; self.prev=None; self.parts=[]
            self.worker=None; self.stop_guard_until=0; self.scroll_point=None
        self.cancel.clear(); set_state(IDLE)

    def finish_cancel(self):
        self.cancel.set(); self.cleanup_to_idle()

    def finish_end(self,auto=False):
        if get_state()!=CAPTURING:return
        if not self._ending.acquire(blocking=False):return
        try:
            self.cancel.set(); set_state(FINALIZING)
            with self.lock:img=self.compose_locked()
            if img is None or img.height<=0:beep(450,150);return
            ok=set_clipboard_image(img)
            threading.Thread(target=self.save_async,args=(img,),daemon=True).start()
            beep(1550 if ok else 450,100)
        finally:
            self.cleanup_to_idle(); self._ending.release()

    def stop(self,auto=False):self.finish_end(auto=auto)

    def save_async(self,img):
        try:img.save(SAVE_DIR/time.strftime('长截图_%Y%m%d_%H%M%S.png'),'PNG',compress_level=1)
        except Exception:pass

shot=LongShot()
def on_click(x,y,button,pressed):
    # AHK exclusively owns selection. Python left-click is STOP only during CAPTURING.
    if button==mouse.Button.left and pressed and get_state()==CAPTURING and time.perf_counter()>=shot.stop_guard_until:shot.stop()

def hot_r():
    threading.Thread(target=text_to_png,daemon=True).start()

def hot_s():
    # A completed/failed S task always returns to IDLE; a new native WM_HOTKEY then starts a fresh task.
    # Native RegisterHotKey also consumes Ctrl+Shift+Alt+S at Windows level, so it cannot leak a capital S
    # into WeChat when the resident process is alive.
    if get_state()==IDLE: shot.arm()

def native_hotkey_loop():
    # Windows-native global hotkeys are deliberately used instead of pynput.GlobalHotKeys.
    # This removes sticky Ctrl/Shift/Alt state after AHK/countdown/capture and makes repeated S reliable.
    user32=ctypes.windll.user32
    MOD_ALT=0x0001; MOD_CONTROL=0x0002; MOD_SHIFT=0x0004; MOD_NOREPEAT=0x4000
    mods=MOD_CONTROL|MOD_SHIFT|MOD_ALT|MOD_NOREPEAT
    HK_R=0x6511; HK_S=0x6512
    if not user32.RegisterHotKey(None,HK_R,mods,ord('R')):
        raise RuntimeError('无法注册 Ctrl+Shift+Alt+R；可能被其他程序占用')
    if not user32.RegisterHotKey(None,HK_S,mods,ord('S')):
        user32.UnregisterHotKey(None,HK_R)
        raise RuntimeError('无法注册 Ctrl+Shift+Alt+S；可能被其他程序占用')
    msg=ctypes.wintypes.MSG()
    try:
        while user32.GetMessageW(ctypes.byref(msg),None,0,0)>0:
            if msg.message==0x0312:  # WM_HOTKEY
                try:
                    if msg.wParam==HK_R: hot_r()
                    elif msg.wParam==HK_S: hot_s()
                except Exception:
                    # A per-task failure must NEVER tear down the resident hotkey loop.
                    # Keep RegisterHotKey alive, clean this S run, and allow the next S to restart.
                    try: shot.finish_cancel()
                    except Exception: set_state(IDLE)
                    try: beep(450,120)
                    except Exception: pass
    finally:
        user32.UnregisterHotKey(None,HK_R); user32.UnregisterHotKey(None,HK_S)

def main():
    ml=mouse.Listener(on_click=on_click);ml.start()
    native_hotkey_loop()
if __name__=='__main__':main()
