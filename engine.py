"""GPU test engine. No GUI imports, privileged operations, or driver changes."""
from __future__ import annotations
import csv
import datetime as dt
import errno
import fcntl
import hashlib
import html
import json
import os
from pathlib import Path
import pty
import re
import select
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
import zipfile

APP = Path(__file__).resolve().parent
# An explicit override takes priority; retain compatibility with existing local installs.
_DEFAULT_TOOLS = APP/'tools'
_LEGACY_TOOLS = APP.parent/'gpu-sale-evidence/tools'
if not _DEFAULT_TOOLS.exists() and _LEGACY_TOOLS.exists():
    _DEFAULT_TOOLS = _LEGACY_TOOLS
TOOLS = Path(os.environ.get('GPU_TESTER_TOOLS', str(_DEFAULT_TOOLS))).expanduser().resolve()
ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]|\x1b\(B')
MEMORY_SECONDS = 305
SESSION_SECONDS = 590  # Leaves ten seconds to finalize recording within 10 minutes.
LIMITS = {'core_c': 85, 'junction_c': 100, 'memory_c': 95}
_HUD_CANDIDATES = (APP/'vendor/mangohud/usr/lib/mangohud/libMangoHud_shim.so',
                   Path('/usr/lib/mangohud/libMangoHud_shim.so'),
                   Path('/usr/lib/x86_64-linux-gnu/mangohud/libMangoHud_shim.so'))
HUD_LIBRARY = Path(os.environ['GPU_TESTER_MANGOHUD_LIBRARY']).expanduser() if os.environ.get('GPU_TESTER_MANGOHUD_LIBRARY') else next((p for p in _HUD_CANDIDATES if p.is_file()), _HUD_CANDIDATES[1])
HUD_CONFIG = ('position=top-left,offset_x=20,offset_y=50,font_size=22,width=320,background_alpha=0.7,'
              'fps,frametime,frame_timing,gpu_stats,gpu_temp,gpu_junction_temp,'
              'gpu_mem_temp,gpu_power,vram,gpu_core_clock,gpu_mem_clock,'
              'cpu_stats=0,fps_limit=0,custom_text_center=GPU Tester')


def overlay_environment(env, enabled=True):
    """Scope OpenGL instrumentation to Heaven; never preload memory or GUI tools."""
    result=env.copy()
    if enabled:
        if not HUD_LIBRARY.is_file():
            raise RuntimeError('MangoHud overlay is missing; install MangoHud or disable Benchmark overlay: '+str(HUD_LIBRARY))
        result['MANGOHUD']='1'
        result['MANGOHUD_DLSYM']='1'
        result['MANGOHUD_CONFIG']=HUD_CONFIG
        result['MANGOHUD_OPENGL_LIBS']=str(HUD_LIBRARY.parent/'libMangoHud_opengl.so')
        result['LD_PRELOAD']=str(HUD_LIBRARY)+(':'+result['LD_PRELOAD'] if result.get('LD_PRELOAD') else '')
    return result


def stamp():
    return dt.datetime.now().astimezone().isoformat()


def command(args, timeout=8, env=None):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT,
                                   timeout=timeout, env=env).strip()


def read(path, default=None):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return default


def number(value):
    try:
        return float(re.search(r'-?\d+(?:\.\d+)?', str(value)).group())
    except (AttributeError, ValueError):
        return None


def camera_devices():
    """Enumerate V4L2 capture devices, excluding metadata-only nodes."""
    if not shutil.which('v4l2-ctl'):
        return []
    devices=[]
    listing=command(['v4l2-ctl','--list-devices'])
    name='Webcam'
    for line in listing.splitlines():
        if line and not line[0].isspace():
            name=line.rstrip(':')
        elif line.strip().startswith('/dev/video'):
            device=line.strip()
            try:
                info=command(['v4l2-ctl','--device',device,'--info'])
                caps=info.split('Device Caps',1)[-1]
                if 'Device Caps' in info and re.search(r'^\s*Video Capture\s*$',caps,re.M):
                    devices.append({'device':device,'name':name})
            except subprocess.SubprocessError:
                continue
    return devices


def camera_mode(device):
    text=command(['v4l2-ctl','--device',device,'--list-formats-ext'])
    modes=[]
    pixel=None
    size=None
    for line in text.splitlines():
        match=re.search(r"\[\d+\]: '([^']+)'",line)
        if match: pixel=match[1]
        match=re.search(r'Size: Discrete (\d+)x(\d+)',line)
        if match: size=tuple(map(int,match.groups()))
        match=re.search(r'\(([\d.]+) fps\)',line)
        if match and size and pixel in ('MJPG','YUYV'):
            modes.append({'width':size[0],'height':size[1],'fps':float(match[1]),'pixel':pixel})
    if not modes:
        raise RuntimeError('No supported MJPEG/YUYV webcam mode found for '+device)
    preferences=[(640,480),(1280,720),(640,360),(960,720),(320,240)]
    def rank(mode):
        size=(mode['width'],mode['height'])
        return (mode['fps']<15,preferences.index(size) if size in preferences else len(preferences),
                mode['pixel']!='MJPG',-min(mode['fps'],30),abs(mode['width']-640))
    return min(modes,key=rank)


class CameraPreview:
    """Own exactly one mpv webcam window; reuse Omarchy's inset window styling."""
    def __init__(self, device, size='Medium', monitor=None):
        if not re.fullmatch(r'/dev/video\d+',device):
            raise ValueError('Select a V4L2 webcam device.')
        self.device,self.size,self.monitor=device,size,monitor
        self.process=None
        self.stream=None
        self.temp=None
        self.info={}

    def alive(self):
        return self.process is not None and self.process.poll() is None

    def start(self):
        if not shutil.which('mpv'):
            raise RuntimeError('Webcam preview requires mpv.')
        mode=camera_mode(self.device)
        monitors=json.loads(command(['hyprctl','-j','monitors']))
        monitor=next((m for m in monitors if m.get('name')==self.monitor),None)
        monitor=monitor or next((m for m in monitors if m.get('focused')),monitors[0] if monitors else None)
        if monitor is None: raise RuntimeError('No monitor available for the webcam inset.')
        self.monitor=monitor['name']
        self.temp=tempfile.TemporaryDirectory(prefix='gpu-tester-webcam-')
        self.log_path=Path(self.temp.name)/'webcam.log'
        self.socket=Path(self.temp.name)/'mpv.sock'
        self.stream=self.log_path.open('w')
        fmt='mjpeg' if mode['pixel']=='MJPG' else 'yuyv422'
        args=['mpv','--no-config','--profile=low-latency','--untimed','--cache=no',
              '--no-audio','--no-osc','--no-input-default-bindings','--no-border',
              '--gpu-api=opengl','--title=WebcamOverlay','--wayland-app-id=WebcamOverlay-medium',
              '--input-ipc-server='+str(self.socket),
              '--demuxer-lavf-o='+f'video_size={mode["width"]}x{mode["height"]},framerate={mode["fps"]:g},input_format={fmt}',
              'av://v4l2:'+self.device]
        self.info={'device':self.device,'mode':mode,'size':self.size,'monitor':self.monitor,
                   'position':'bottom-right','uncropped':True,'command':args}
        try:
            self.process=subprocess.Popen(args,stdout=self.stream,stderr=subprocess.STDOUT,start_new_session=True)
            deadline=time.monotonic()+8
            window=None
            while time.monotonic()<deadline:
                if not self.alive():
                    raise RuntimeError('Webcam failed to open; it may be disconnected or in use. '+read(self.log_path,''))
                clients=json.loads(command(['hyprctl','-j','clients'],timeout=2))
                window=next((w for w in clients if w.get('pid')==self.process.pid and w.get('mapped')),None)
                if window: break
                time.sleep(.1)
            if window is None: raise RuntimeError('Timed out waiting for webcam preview.')
            scale=monitor.get('scale',1) or 1
            logical_width=monitor['width']/scale
            logical_height=monitor['height']/scale
            fraction={'Small':.18,'Medium':.25,'Large':.34}.get(self.size,.25)
            width=round(logical_width*fraction)
            height=round(width*mode['height']/mode['width'])
            x=round(monitor['x']+logical_width-width-24)
            y=round(monitor['y']+logical_height-height-48)
            target='address:'+window['address']
            # These Lua dispatchers come from this installed Omarchy version.
            for method,fields in [('resize',f'x = {width}, y = {height}'),('move',f'x = {x}, y = {y}')]:
                command(['hyprctl','eval',f'hl.dispatch(hl.dsp.window.{method}({{ window = "{target}", {fields} }}))'])
        except Exception:
            self.close()
            raise

    def close(self, log_destination=None):
        stop_process(self.process,timeout=1)
        if self.stream: self.stream.close()
        if self.temp:
            if log_destination and self.log_path.exists(): shutil.copyfile(self.log_path,log_destination)
            self.temp.cleanup()
        self.process=None
        self.stream=None
        self.temp=None


class GPU:
    def __init__(self, path):
        self.path = Path(path)
        self.pci = self.path.name
        self.vendor = 'NVIDIA' if read(self.path/'vendor') == '0x10de' else 'AMD'
        self.hwmon = next(iter((self.path/'hwmon').glob('*')), None)
        self.info = self.identity()

    def identity(self):
        info = {'vendor': self.vendor, 'pci_address': self.pci,
                'manufacturer_serial': None, 'serial_note': 'Manufacturer label serial is unavailable.',
                'kernel': os.uname().release}
        for key in ['vendor','device','subsystem_vendor','subsystem_device','vbios_version',
                    'unique_id','mem_info_vram_total','current_link_speed','current_link_width']:
            info['sysfs_'+key] = read(self.path/key)
        info['pci_description'] = command(['lspci','-nnk','-s',self.pci])
        driver = self.path/'driver'
        info['driver'] = driver.resolve().name if driver.exists() else None
        if self.vendor == 'NVIDIA':
            xml = command(['nvidia-smi','-i',self.pci,'-q','-x'])
            gpu = ET.fromstring(xml).find('gpu')
            if gpu is None:
                raise RuntimeError('NVIDIA driver returned no GPU identity.')
            info.update(model=gpu.findtext('product_name'), uuid=gpu.findtext('uuid'),
                        vbios=gpu.findtext('vbios_version'), vram=gpu.findtext('fb_memory_usage/total'),
                        driver_version=ET.fromstring(xml).findtext('driver_version'))
            serial = gpu.findtext('serial')
            if serial and serial not in ('N/A','Unknown','Not Supported'):
                info['manufacturer_serial'] = serial
                info['serial_note'] = 'Serial reported by NVIDIA hardware interface.'
            info['identity_xml'] = xml
        else:
            description = info['pci_description'].splitlines()[0]
            subsystem = next((x.strip() for x in info['pci_description'].splitlines() if 'Subsystem:' in x), '')
            model = re.sub(r'\s*\[[0-9a-f:]+\]|\s*\(rev .*?\)', '', subsystem.removeprefix('Subsystem:').strip() or description.split(': ',1)[-1])
            info['model'] = model.replace('Micro-Star International Co., Ltd. [MSI]', 'MSI')
            total = number(read(self.path/'mem_info_vram_total'))
            info['vram'] = f'{total / 2**30:.1f} GiB' if total else 'Unavailable'
            info['vbios'] = read(self.path/'vbios_version')
            serial = read(self.path/'serial_number')
            if serial and serial.lower() not in ('0','0x0','n/a','unknown'):
                info['manufacturer_serial'] = serial
                info['serial_note'] = 'Serial reported by AMD sysfs serial_number interface.'
        return info

    def sample(self):
        row = {'timestamp': stamp(), 'core_c':None, 'junction_c':None, 'memory_c':None,
               'gpu_load_pct':None, 'vram_used_mib':None, 'power_w':None, 'fan':None}
        if self.vendor == 'NVIDIA':
            fields = 'temperature.gpu,utilization.gpu,memory.used,power.draw,fan.speed'
            data = next(csv.reader([command(['nvidia-smi','-i',self.pci,'--query-gpu='+fields,'--format=csv,noheader,nounits'], timeout=3)]))
            for key, value in zip(['core_c','gpu_load_pct','vram_used_mib','power_w','fan'],data):
                row[key] = number(value)
            row['fan_unit'] = '%'
        else:
            if not self.hwmon:
                raise RuntimeError('AMD temperature sensors unavailable; cannot monitor safely.')
            for path in self.hwmon.glob('temp*_input'):
                label = read(path.with_name(path.name.replace('_input','_label')), '').lower()
                key = {'edge':'core_c','gpu':'core_c','junction':'junction_c','mem':'memory_c'}.get(label)
                if not key and path.name=='temp1_input': key='core_c'
                if key:
                    value = number(read(path))
                    row[key] = value/1000 if value is not None else None
            row['gpu_load_pct'] = number(read(self.path/'gpu_busy_percent'))
            used = number(read(self.path/'mem_info_vram_used'))
            row['vram_used_mib'] = used/2**20 if used is not None else None
            power = number(read(self.hwmon/'power1_average'))
            row['power_w'] = power/1e6 if power is not None else None
            row['fan'] = number(read(self.hwmon/'fan1_input'))
            row['fan_unit'] = 'RPM'
        row['pcie_speed'] = read(self.path/'current_link_speed')
        row['pcie_width'] = read(self.path/'current_link_width')
        if row['core_c'] is None:
            raise RuntimeError('Core temperature unavailable; test stopped for monitoring failure.')
        return row

    def environment(self):
        env = os.environ.copy()
        env['VK_LOADER_DEBUG'] = 'error'
        env.pop('VK_ICD_FILENAMES',None)
        if self.vendor == 'AMD':
            system = Path('/usr/share/vulkan/icd.d/radeon_icd.json')
            portable = TOOLS/'radeon-driver/usr/share/vulkan/icd.d/radeon_icd.json'
            icd = system if system.exists() else portable
            if not icd.exists():
                raise RuntimeError('AMD Vulkan driver missing. Install vulkan-radeon with Omarchy package management.')
            env['VK_DRIVER_FILES'] = str(icd)
            env['__GLX_VENDOR_LIBRARY_NAME'] = 'mesa'
            env['DRI_PRIME'] = 'pci-'+self.pci.replace(':','_').replace('.','_')
            libraries = [TOOLS/'heaven/bin', TOOLS/'heaven/bin/x64']
            if icd == portable:
                libraries.insert(0,TOOLS/'radeon-driver/usr/lib')
        else:
            icd = Path('/usr/share/vulkan/icd.d/nvidia_icd.json')
            if not icd.exists():
                raise RuntimeError('NVIDIA Vulkan driver manifest is missing.')
            env['VK_DRIVER_FILES'] = str(icd)
            env['__GLX_VENDOR_LIBRARY_NAME'] = 'nvidia'
            env['__NV_PRIME_RENDER_OFFLOAD'] = '1'
            libraries = [TOOLS/'heaven/bin', TOOLS/'heaven/bin/x64']
        env['LD_LIBRARY_PATH'] = ':'.join(map(str,libraries))+':'+env.get('LD_LIBRARY_PATH','')
        return env


def discover():
    paths = [p for p in Path('/sys/bus/pci/devices').iterdir()
             if read(p/'vendor') in ('0x1002','0x10de') and (read(p/'class','')).startswith('0x03')]
    if len(paths) != 1:
        raise RuntimeError(f'Found {len(paths)} AMD/NVIDIA GPUs. Connect exactly one supported GPU so results cannot be attributed to the wrong card.')
    return GPU(paths[0])


def memory_verdict(log, result):
    if 'Error found' in log or 'testing failed' in log.lower():
        return 'FAIL'
    if result['stop_reason'] in ('cancelled','temperature limit','monitoring failure','session deadline','recording failure','webcam failure'):
        return 'INCOMPLETE'
    if result['elapsed_seconds'] >= 300 and 'no any errors, testing PASSed' in log:
        return 'PASS'
    return 'INCOMPLETE' if result['stop_reason']=='stage duration' else 'TOOL ERROR'


def graphics_verdict(log, result, gpu):
    errors = bool(re.search(r'OpenGL error:|incomplete attachment|fatal error|GLRender::.*failed',log,re.I))
    match = re.search(r'FPS:\s*([\d.]+)',log)
    renderer = re.search(r'OpenGL renderer:\s*(.+)',log)
    correct = bool(renderer and (('NVIDIA' in renderer[1]) if gpu.vendor=='NVIDIA' else ('AMD' in renderer[1] or 'ATI' in renderer[1])))
    if errors:
        return 'TOOL ERROR', None
    if result.get('stop_reason'):
        return 'INCOMPLETE', None
    if not correct:
        return 'TOOL ERROR', None
    if result['return_code']==0 and result['stop_reason'] is None and 'Phoronix finished' in log and match:
        return 'PASS', float(match[1])
    return 'INCOMPLETE', None


def stop_process(process, sig=signal.SIGTERM, timeout=2):
    if process and process.poll() is None:
        try:
            os.killpg(process.pid,sig)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=2)


class Runner:
    def __init__(self, gpu, output, emit, record=False, reports=True,
                 memory_seconds=MEMORY_SECONDS, session_seconds=SESSION_SECONDS, overlay=True,
                 webcam_device=None, webcam_size='Medium'):
        self.gpu, self.output, self.emit = gpu, Path(output), emit
        self.record, self.reports = record, reports
        self.overlay=overlay
        self.webcam_device,self.webcam_size=webcam_device,webcam_size
        self.webcam=None
        self.memory_seconds = min(memory_seconds,MEMORY_SECONDS)
        self.session_seconds = min(session_seconds,SESSION_SECONDS)
        self.cancel = threading.Event()
        self.process = self.recorder = None
        self.record_stream = None
        self.started = None
        self.lock = None
        self.result = {'gpu':gpu.info, 'started':stamp(), 'tests':{}, 'samples':[],
                       'recording_requested':record, 'reports_requested':reports,
                       'overlay_requested':overlay,
                       'webcam_requested':bool(webcam_device),
                       'limits_c':LIMITS, 'status':'INCOMPLETE', 'warnings':[]}

    def log(self, text):
        self.emit('log',text)

    def acquire(self):
        runtime = Path(os.environ.get('XDG_RUNTIME_DIR',tempfile.gettempdir()))
        self.lock = (runtime/f'gpu-tester-{os.getuid()}.lock').open('a')
        try:
            fcntl.flock(self.lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            self.lock = None
            raise RuntimeError('Another GPU Tester session is running.')

    def start_recording(self):
        if not shutil.which('gpu-screen-recorder'):
            raise RuntimeError('Screen recording requires gpu-screen-recorder.')
        monitors = json.loads(command(['hyprctl','-j','monitors']))
        monitor = (next((m for m in monitors if m.get('name')==self.webcam.monitor),None)
                   if self.webcam else next((m for m in monitors if m.get('focused')), monitors[0] if monitors else None))
        if monitor is None:
            raise RuntimeError('No active Hyprland monitor found for recording.')
        self.record_stream = (self.raw/'recording.log').open('w')
        args = ['gpu-screen-recorder','-w',monitor['name'],'-f','30','-k','h264','-q','medium',
                '-s','1920x1080','-o',str(self.dest/'test-recording.mp4')]
        self.recorder = subprocess.Popen(args,stdout=self.record_stream,stderr=subprocess.STDOUT,start_new_session=True)
        self.result['recording_command'] = args
        self.log('Recording '+monitor['name']+' at 30 FPS; desktop capture includes the test window.\n')
        until = time.monotonic()+2
        while time.monotonic()<until and not self.cancel.is_set():
            if self.recorder.poll() is not None:
                raise RuntimeError('Recorder failed to start. See raw/recording.log.')
            time.sleep(.1)

    def stage(self, name, args, cwd, duration, stop_signal, env=None):
        self.emit('stage',name)
        self.log('\n── '+name.upper()+' ──\n')
        start = time.monotonic()
        result = {'started':stamp(),'command':args,'stop_reason':None}
        master, slave = pty.openpty()
        try:
            self.process = subprocess.Popen(args,cwd=cwd,env=env if env is not None else self.env,stdin=subprocess.DEVNULL,
                                            stdout=slave,stderr=slave,start_new_session=True)
        finally:
            os.close(slave)
        deadline = min(start+duration,self.deadline)
        next_sample = 0
        try:
            with (self.raw/(name+'.log')).open('w') as logfile, (self.raw/(name+'-telemetry.csv')).open('w') as csvfile:
                writer = None
                while self.process.poll() is None:
                    clock = time.monotonic()
                    if self.cancel.is_set():
                        result['stop_reason']='cancelled'
                        break
                    if self.recorder and self.recorder.poll() is not None:
                        result['stop_reason']='recording failure'
                        self.result['warnings'].append('Recorder exited during testing; capture may be incomplete.')
                        break
                    if self.webcam and not self.webcam.alive():
                        result['stop_reason']='webcam failure'
                        self.result['warnings'].append('Webcam preview exited during testing; camera evidence is incomplete.')
                        break
                    if clock>=deadline:
                        result['stop_reason']='session deadline' if clock>=self.deadline else 'stage duration'
                        break
                    if clock>=next_sample:
                        try:
                            row=self.gpu.sample()
                        except Exception as error:
                            self.log(str(error)+'\n')
                            result['stop_reason']='monitoring failure'
                            break
                        row.update(stage=name,elapsed_seconds=round(clock-self.started,2))
                        if writer is None:
                            writer=csv.DictWriter(csvfile,fieldnames=list(row))
                            writer.writeheader()
                        writer.writerow(row)
                        csvfile.flush()
                        self.result['samples'].append(row)
                        self.emit('sample',row)
                        if any(row.get(key) is not None and row[key]>=limit for key,limit in LIMITS.items()):
                            result['stop_reason']='temperature limit'
                            break
                        next_sample=clock+1
                    if select.select([master],[],[],.15)[0]:
                        try:
                            data=os.read(master,65536)
                        except OSError as error:
                            if error.errno==errno.EIO: break
                            raise
                        text=ANSI.sub('',data.decode(errors='replace')).replace('\r','')
                        logfile.write(text)
                        logfile.flush()
                        self.log(text)
                        if name=='memory' and ('Error found' in text or 'testing failed' in text.lower()):
                            result['stop_reason']='memory errors'
                            break
                stop_process(self.process,stop_signal,timeout=1)
                # Drain the PTY: memtest prints its final PASS only after SIGINT.
                while select.select([master],[],[],.1)[0]:
                    try:
                        data=os.read(master,65536)
                    except OSError:
                        break
                    if not data: break
                    text=ANSI.sub('',data.decode(errors='replace')).replace('\r','')
                    logfile.write(text)
                    self.log(text)
        finally:
            stop_process(self.process,stop_signal,timeout=1)
            os.close(master)
        result.update(ended=stamp(),elapsed_seconds=round(time.monotonic()-start,2),return_code=self.process.returncode)
        self.process=None
        log=(self.raw/(name+'.log')).read_text()
        if name=='memory':
            result['status']=memory_verdict(log,result)
            if 'Failed to allocate a buffer' in log:
                self.result['warnings'].append('Memory tool reduced its allocation after larger buffers failed. PASS does not establish full VRAM coverage.')
        else:
            result['status'],result['fps']=graphics_verdict(log,result,self.gpu)
            if 'Unknown command "shaders_tessellation"' in log:
                self.result['warnings'].append('Normal tessellation was requested; Heaven rejected its shaders_tessellation command, so the active tessellation level is unconfirmed.')
        self.result['tests'][name]=result
        (self.raw/(name+'-metadata.json')).write_text(json.dumps(result,indent=2))
        self.emit('verdict',{'stage':name,**result})
        self.log(f'\n{name.capitalize()}: {result["status"]} ({result["elapsed_seconds"]:.1f}s)\n')
        if result['stop_reason'] and result['stop_reason']!='stage duration':
            self.log('Stopped: '+result['stop_reason']+'\n')
        return result

    def watchdog(self):
        if not self.finished.wait(max(0,self.deadline-time.monotonic())):
            self.cancel.set()
            self.log('\nSession deadline reached; stopping tests and recording.\n')
            for process in (self.process,self.recorder,self.webcam.process if self.webcam else None):
                if process and process.poll() is None:
                    try: os.killpg(process.pid,signal.SIGINT if process is self.recorder else signal.SIGTERM)
                    except ProcessLookupError: pass

    def run(self):
        self.finished=threading.Event()
        try:
            self.acquire()
            self.output.mkdir(parents=True,exist_ok=True)
            self.dest=Path(tempfile.mkdtemp(prefix=dt.datetime.now().strftime('%Y%m%d-%H%M%S-')+self.gpu.vendor+'-',dir=self.output))
            self.raw=self.dest/'raw'
            self.raw.mkdir()
            self.emit('destination',str(self.dest))
            self.env=self.gpu.environment()
            self.gpu.sample()  # Refuse to stress the card without working temperature monitoring.
            for tool in (TOOLS/'memtest_vulkan',TOOLS/'heaven/bin/heaven_x64'):
                if not tool.is_file() or not os.access(tool,os.X_OK):
                    raise RuntimeError('Test tool unavailable: '+str(tool))
            (self.raw/'identity-before.json').write_text(json.dumps(self.gpu.info,indent=2))
            if self.webcam_device:
                if not self.record:
                    raise RuntimeError('Enable Record screen to include webcam evidence.')
                self.webcam=CameraPreview(self.webcam_device,self.webcam_size)
                self.webcam.start()
                self.result['webcam']=self.webcam.info
                self.log('Live webcam inset ready: '+self.webcam_device+' (bottom-right, full camera image).\n')
            self.started=time.monotonic()
            self.deadline=self.started+self.session_seconds
            self.emit('started',self.started)
            threading.Thread(target=self.watchdog,daemon=True).start()
            if self.record: self.start_recording()
            if not self.cancel.is_set():
                memory=self.stage('memory',[str(TOOLS/'memtest_vulkan')],TOOLS,self.memory_seconds,signal.SIGINT)
                if memory['status']=='PASS' and not self.cancel.is_set():
                    args=[str(TOOLS/'heaven/bin/heaven_x64'),'-data_path','../','-sound_app','null',
                          '-engine_config','../data/heaven_4.0.cfg','-system_script','heaven/unigine.cpp',
                          '-video_mode','-1','-extern_define','PHORONIX,RELEASE,QUALITY_ULTRA,TESSELLATION_NORMAL',
                          '-video_app','opengl','-video_fullscreen','0','-video_width','1920',
                          '-video_height','1080','-video_multisample','3']
                    graphics_env=overlay_environment(self.env,self.overlay)
                    if self.overlay:
                        self.log('MangoHud overlay: FPS, frame time, VRAM, GPU load, temperatures, clocks and power.\n')
                        self.result['overlay_config']=HUD_CONFIG
                    self.stage('graphics',args,TOOLS/'heaven',self.session_seconds,signal.SIGTERM,env=graphics_env)
                else:
                    self.log('Graphics skipped because memory did not pass or the run was cancelled.\n')
            after=self.gpu.identity()
            (self.raw/'identity-after.json').write_text(json.dumps(after,indent=2))
            keys=['vendor','pci_address','uuid','vbios','sysfs_device','sysfs_subsystem_device','sysfs_unique_id']
            if any(after.get(k)!=self.gpu.info.get(k) for k in keys):
                raise RuntimeError('GPU identity changed during testing; results cannot be verified.')
        except Exception as error:
            self.result['error']=str(error)
            self.log('\nERROR: '+str(error)+'\n')
        finally:
            stop_process(self.process,signal.SIGTERM,timeout=1)
            stop_process(self.recorder,signal.SIGINT,timeout=5)
            if self.webcam: self.webcam.close(self.raw/'webcam.log')
            self.finished.set()
            if self.record_stream: self.record_stream.close()
            if self.started:
                self.result['testing_and_capture_seconds']=round(time.monotonic()-self.started,2)
            self.result['ended']=stamp()
            tests=self.result['tests']
            if any(t['status']=='FAIL' for t in tests.values()):
                self.result['status']='FAIL'
            elif self.result.get('error') or any(t['status']=='TOOL ERROR' for t in tests.values()):
                self.result['status']='TOOL ERROR'
            elif len(tests)==2 and all(t['status']=='PASS' for t in tests.values()):
                self.result['status']='PASS'
            if getattr(self,'dest',None):
                try:
                    self.finalize()
                except Exception as error:
                    self.result['warnings'].append('Export failed: '+str(error))
                    (self.dest/'results.json').write_text(json.dumps(self.result,indent=2))
                    self.log('Export error: '+str(error)+'\n')
            if self.lock: self.lock.close()
            self.emit('done',{'result':self.result,'destination':str(getattr(self,'dest',''))})

    def finalize(self):
        self.emit('stage','export')
        journal=subprocess.run(['journalctl','-k','--since',self.result['started'],'--no-pager'],capture_output=True,text=True,timeout=8)
        events='\n'.join(x for x in journal.stdout.splitlines() if any(s in x.lower() for s in ['nvrm','xid','amdgpu','aer:','pcie bus error','gpu reset','ring timeout']))
        (self.raw/'gpu-kernel-events.txt').write_text(events+'\n'+journal.stderr)
        self.result['kernel_log_accessible']=journal.returncode==0 and not journal.stderr.strip()
        self.result['kernel_events']=events
        if self.record:
            video=self.dest/'test-recording.mp4'
            if video.exists() and video.stat().st_size:
                try:
                    duration=float(command(['ffprobe','-v','error','-show_entries','format=duration','-of','default=nw=1:nk=1',str(video)]))
                    self.result['video_seconds']=duration
                    self.result['recording_valid']=bool(self.recorder and self.recorder.returncode==0 and duration<=600)
                    if not self.result['recording_valid']:
                        self.result['warnings'].append('Recording is incomplete, invalid, or exceeds the ten-minute cap.')
                except Exception as error:
                    self.result['recording_valid']=False
                    self.result['warnings'].append('Video validation failed: '+str(error))
            else:
                self.result['recording_valid']=False
        summary=self.dest/'results.json'
        summary.write_text(json.dumps(self.result,indent=2))
        if self.reports:
            write_html(self.dest,self.result)
            if not shutil.which('chromium'):
                raise RuntimeError('PDF export requires Chromium. HTML and JSON are saved.')
            with tempfile.TemporaryDirectory(prefix='gpu-tester-print-') as profile:
                subprocess.run(['chromium','--headless','--no-sandbox','--disable-gpu','--no-pdf-header-footer',
                                '--user-data-dir='+profile,'--print-to-pdf='+str(self.dest/'GPU-test-report.pdf'),
                                (self.dest/'GPU-test-report.html').as_uri()],stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL,timeout=45,check=True)
        files=[p for p in self.dest.rglob('*') if p.is_file() and p.name not in ('SHA256SUMS','GPU-evidence.zip')]
        checksums=self.dest/'SHA256SUMS'
        def checksum(path):
            with path.open('rb') as stream:
                return hashlib.file_digest(stream,'sha256').hexdigest()
        checksums.write_text(''.join(checksum(p)+'  '+str(p.relative_to(self.dest))+'\n' for p in sorted(files)))
        with zipfile.ZipFile(self.dest/'GPU-evidence.zip','w',zipfile.ZIP_DEFLATED) as archive:
            for path in files+[checksums]:
                archive.write(path,path.relative_to(self.dest),compress_type=zipfile.ZIP_STORED if path.suffix=='.mp4' else zipfile.ZIP_DEFLATED)
        self.log('Saved results, logs, checksums'+(', HTML and PDF' if self.reports else '')+'.\n')


def write_html(dest, result):
    esc=lambda x:html.escape(str(x))
    gpu={k:v for k,v in result['gpu'].items() if k!='identity_xml'}
    tests=result['tests']
    samples=result['samples']
    rows=''.join(f'<tr><td>{esc(name.title())}</td><td>{esc(t["status"])}</td><td>{t["elapsed_seconds"]:.1f}s</td><td>{esc(t.get("fps","—"))}</td><td>{esc(t.get("stop_reason") or "Completed")}</td></tr>' for name,t in tests.items())
    peaks={key:max((s[key] for s in samples if s.get(key) is not None),default=None) for key in ['core_c','junction_c','memory_c','power_w','vram_used_mib']}
    warnings=''.join('<li>'+esc(x)+'</li>' for x in result['warnings'])
    logs=''
    for name in tests:
        text=(dest/'raw'/(name+'.log')).read_text()
        excerpt='\n'.join(text.splitlines()[-12:])
        logs+=f'<h3>{esc(name.title())}</h3><pre>{esc(excerpt)}</pre>'
    status=esc(result['status'])
    body=f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><title>GPU test — {esc(gpu['model'])}</title><style>
    @page{{size:A4;margin:15mm}}body{{font:14px Arial,sans-serif;color:#182536;max-width:960px;margin:36px auto;padding:0 24px}}h1{{font-size:30px}}h2{{margin-top:28px;font-size:18px}}.badge{{padding:16px;background:#edf3fa;border-left:5px solid #4279c7}}table{{border-collapse:collapse;width:100%}}td,th{{padding:9px;text-align:left;border-bottom:1px solid #dde3eb}}pre{{font-size:10px;background:#f4f6f9;padding:12px;white-space:pre-wrap;overflow-wrap:anywhere}}.muted{{color:#627184}}tr,pre{{break-inside:avoid}}</style></head><body>
    <p class="muted">GPU TESTER · LOCAL TEST EVIDENCE</p><h1>{esc(gpu['model'])}</h1><p>{esc(gpu.get('vram'))} · {esc(gpu['pci_address'])} · Started {esc(result['started'])}</p>
    <div class="badge"><b>Overall result: {status}</b><br>Testing and capture: {esc(result.get('testing_and_capture_seconds','Not started'))} seconds. Recording: {esc(result.get('video_seconds','Not requested'))} seconds.</div>
    <h2>Results</h2><table><tr><th>Test</th><th>Result</th><th>Duration</th><th>FPS</th><th>Stop reason</th></tr>{rows}</table>
    <p>Memory: memtest_vulkan, approximately five minutes of available VRAM patterns. Graphics: Heaven 4.0 OpenGL, requested 1920×1080 windowed, Ultra, 8× AA, normal tessellation. A score is reported only for a completed benchmark without recorded framebuffer errors. MangoHud overlay requested: {esc(result.get('overlay_requested',False))}. Overlay rendering can slightly affect the score; sensor availability depends on the GPU and driver.</p>
    <h2>Peak telemetry</h2><pre>{esc(json.dumps(peaks,indent=2))}</pre><p>Temperatures in °C, VRAM use in MiB, power in watts. One-second sampling; power and PCIe data reflect driver readings and this host. Unavailable sensors are null.</p>
    <h2>Identity</h2><pre>{esc(json.dumps(gpu,indent=2))}</pre><p>Manufacturer serial: {esc(gpu.get('manufacturer_serial') or 'Unavailable')}. UUID and PCI address are separate identifiers and are never substituted for a label serial.</p>
    <h2>Webcam evidence</h2><p>Webcam inset requested: {esc(result.get('webcam_requested',False))}. Camera: {esc(result.get('webcam',{}).get('device','Not used'))}. The live camera view preserves its aspect ratio and is captured with the desktop in the same video. The footage may help connect the physical card to the test; it does not automatically verify the card's label or serial.</p>
    <h2>Warnings and limitations</h2><ul>{warnings or '<li>No additional tool warnings.</li>'}</ul><p>{esc(result.get('error',''))}</p>
    <p>PASS records the tests that completed. It does not guarantee future reliability, test every byte of installed VRAM, verify physical condition or every output port, or provide a human assessment of artifacts or fan noise. Recording adds encoding load, so recorded scores may differ. Pair results with photos of the actual card and its serial label.</p>
    <h2>Raw result excerpts</h2>{logs}<h2>Kernel GPU events</h2><pre>{esc(result.get('kernel_events') or ('No matching events recorded.' if result.get('kernel_log_accessible') else 'Kernel log unavailable or restricted.'))}</pre>
    <h2>Evidence files</h2><p>Original logs, telemetry CSVs, identity JSON, results.json and SHA256SUMS accompany this report. Checksums detect changes relative to a trusted copy; they are not a certification or signature.</p></body></html>'''
    (dest/'GPU-test-report.html').write_text(body)
