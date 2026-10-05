#!/usr/bin/env python3
"""Native GTK4 GPU Tester for an Omarchy desktop."""
import json
import math
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import gi
gi.require_version('Gtk','4.0')
gi.require_version('Adw','1')
from gi.repository import Adw, Gdk, Gio, GLib, Gtk
from engine import APP, MEMORY_SECONDS, SESSION_SECONDS, Runner, discover, CameraPreview, camera_devices

CSS = '''
window { background: #0d1420; color: #e7edf7; }
.shell { padding: 24px; }
.eyebrow { color: #77a8fa; font-size: 11px; font-weight: 800; letter-spacing: 2px; }
.title { font-size: 29px; font-weight: 800; }
.muted { color: #8e9fb8; font-size: 12px; }
.panel { background: #151f2f; border: 1px solid #25334a; border-radius: 14px; padding: 16px; }
.gpu-name { font-size: 17px; font-weight: 700; }
.status { color: #78d5b5; background: #1b3a37; border-radius: 20px; padding: 7px 12px; font-size: 12px; font-weight: 700; }
.metric { font-size: 26px; font-weight: 750; }
.metric-label { color: #9caec8; font-size: 11px; }
.timer { font-size: 30px; font-weight: 800; font-family: monospace; }
.stage { color: #aec8f0; font-weight: 700; }
button { background: #233148; color: #e7edf7; border: 0; border-radius: 9px; padding: 9px 15px; }
button.primary { background: #397dea; font-weight: 800; padding: 12px 26px; }
button.danger { background: #59313d; color: #ffd0d8; }
button:disabled { opacity: .45; }
switch:checked { background: #397dea; }
textview, textview text { background: #0f1826; color: #becce0; font: 11px monospace; }
progressbar trough { background: #25334a; min-height: 7px; border-radius: 5px; }
progressbar progress { background: #528ffd; min-height: 7px; border-radius: 5px; }
.footer { color: #7f92b0; font-size: 11px; }
'''


def label(text='',css=None,xalign=0):
    item=Gtk.Label(label=text,xalign=xalign)
    if css: item.add_css_class(css)
    return item


class Window(Adw.ApplicationWindow):
    def __init__(self,app):
        super().__init__(application=app,title='GPU Tester',default_width=1060,default_height=860)
        self.events=queue.Queue()
        self.gpu=None
        self.runner=None
        self.active=False
        self.pending_cancel=False
        self.inhibit_cookie=0
        self.started=None
        self.destination=None
        self.samples=[]
        self.export_options=[]
        self.cameras=[]
        self.camera_preview=None
        self.camera_busy=False
        self.output=APP/'results'
        css=Gtk.CssProvider()
        css.load_from_data(CSS.encode())
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(),css,Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        shell=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=16)
        shell.add_css_class('shell')
        root=Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer=Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,vexpand=True)
        outer.set_child(shell)
        root.append(outer)
        self.set_content(root)
        heading=Gtk.Box(spacing=20)
        titles=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=4,hexpand=True)
        titles.append(label('GPU TESTER','eyebrow'))
        titles.append(label('Put your next card to the test.','title'))
        titles.append(label('Live evidence. One click. Under ten minutes.','muted'))
        heading.append(titles)
        self.status=label('Detecting…','status')
        self.status.set_valign(Gtk.Align.CENTER)
        heading.append(self.status)
        shell.append(heading)

        hardware=Gtk.Box(spacing=16)
        hardware.add_css_class('panel')
        icon=Gtk.Image.new_from_icon_name('video-display-symbolic')
        icon.set_pixel_size(34)
        hardware.append(icon)
        names=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=5,hexpand=True)
        self.name=label('Looking for an AMD or NVIDIA GPU…','gpu-name')
        self.name.set_wrap(True)
        self.identity=label('','muted')
        self.identity.set_wrap(True)
        names.append(self.name)
        names.append(self.identity)
        hardware.append(names)
        self.refresh=Gtk.Button(label='Refresh GPU')
        self.refresh.connect('clicked',self.detect)
        hardware.append(self.refresh)
        shell.append(hardware)

        metrics=Gtk.Box(spacing=12,homogeneous=True)
        self.metrics={}
        for key,title in [('core_c','CORE TEMP'),('junction_c','JUNCTION'),('gpu_load_pct','GPU LOAD'),('power_w','POWER'),('vram_used_mib','VRAM USED')]:
            box=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=7)
            box.add_css_class('panel')
            box.append(label(title,'metric-label'))
            value=label('—','metric')
            self.metrics[key]=value
            box.append(value)
            metrics.append(box)
        shell.append(metrics)

        progress=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=10)
        progress.add_css_class('panel')
        row=Gtk.Box(spacing=12)
        description=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=5,hexpand=True)
        self.stage=label('Ready when you are','stage')
        self.stage_details=label('01  Memory · 5 minutes     →     02  Graphics · remaining time     →     03  Save results','muted')
        self.stage_details.set_wrap(True)
        description.append(self.stage)
        description.append(self.stage_details)
        row.append(description)
        self.timer=label('00:00','timer',1)
        row.append(self.timer)
        progress.append(row)
        self.progress=Gtk.ProgressBar()
        progress.append(self.progress)
        self.graph=Gtk.DrawingArea(content_height=78)
        self.graph.set_draw_func(self.draw_graph)
        progress.append(self.graph)
        graph_footer=Gtk.Box(hexpand=True)
        graph_footer.append(label('● Core temperature     ● Junction temperature','muted'))
        self.remaining=label('10:00 maximum', 'muted',1)
        self.remaining.set_hexpand(True)
        graph_footer.append(self.remaining)
        progress.append(graph_footer)
        shell.append(progress)

        options=Gtk.Grid(column_spacing=18,row_spacing=12)
        for index,(text,detail,value) in enumerate([('Record screen','Focused monitor · MP4 · 30 FPS',False),('Export reports','HTML + PDF + evidence ZIP',True),('Benchmark overlay','FPS · VRAM · temps · power',True),('Webcam PiP','Physical card view · bottom-right inset',False)]):
            box=Gtk.Box(spacing=12,hexpand=True)
            words=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=4,hexpand=True)
            words.append(label(text))
            words.append(label(detail,'muted'))
            box.append(words)
            switch=Gtk.Switch(active=value,valign=Gtk.Align.CENTER)
            box.append(switch)
            self.export_options.append(switch)
            options.attach(box,index%2,index//2,1,1)
        shell.append(options)
        self.export_options[3].connect('notify::active',self.webcam_toggled)
        self.export_options[0].connect('notify::active',self.record_toggled)
        camera_row=Gtk.Box(spacing=10)
        camera_row.append(label('Camera','muted'))
        self.camera_select=Gtk.DropDown.new_from_strings(['Detecting camera…'])
        self.camera_select.set_hexpand(True)
        self.camera_select.set_sensitive(False)
        self.camera_select.connect('notify::selected',self.camera_changed)
        camera_row.append(self.camera_select)
        self.camera_size=Gtk.DropDown.new_from_strings(['Small','Medium','Large'])
        self.camera_size.set_selected(1)
        self.camera_size.connect('notify::selected',self.camera_changed)
        camera_row.append(self.camera_size)
        self.preview_button=Gtk.Button(label='Preview camera',sensitive=False)
        self.preview_button.connect('clicked',self.toggle_preview)
        camera_row.append(self.preview_button)
        self.camera_refresh=Gtk.Button(label='Refresh cameras')
        self.camera_refresh.connect('clicked',self.detect_cameras)
        camera_row.append(self.camera_refresh)
        shell.append(camera_row)

        logs_header=Gtk.Box(spacing=12)
        logs_header.append(label('LIVE TEST LOG','eyebrow'))
        self.log_note=label('Temperature guards active · Stop at any time','muted',1)
        self.log_note.set_hexpand(True)
        logs_header.append(self.log_note)
        shell.append(logs_header)
        self.logview=Gtk.TextView(editable=False,cursor_visible=False,monospace=True,
                                  wrap_mode=Gtk.WrapMode.WORD_CHAR,left_margin=12,right_margin=12,
                                  top_margin=10,bottom_margin=10)
        self.buffer=self.logview.get_buffer()
        self.log_end=self.buffer.create_mark('log-end',self.buffer.get_end_iter(),False)
        scroller=Gtk.ScrolledWindow(vexpand=True,min_content_height=150)
        scroller.set_child(self.logview)
        shell.append(scroller)

        actions=Gtk.Box(spacing=10)
        self.start=Gtk.Button(label='▶  Test GPU')
        self.start.add_css_class('primary')
        self.start.set_sensitive(False)
        self.start.connect('clicked',self.begin)
        actions.append(self.start)
        self.stop=Gtk.Button(label='Stop test',sensitive=False)
        self.stop.add_css_class('danger')
        self.stop.connect('clicked',self.cancel_run)
        actions.append(self.stop)
        spacer=Gtk.Box(hexpand=True)
        actions.append(spacer)
        self.report=Gtk.Button(label='Open report',sensitive=False)
        self.report.connect('clicked',self.open_report)
        actions.append(self.report)
        self.folder=Gtk.Button(label='Open results',sensitive=False)
        self.folder.connect('clicked',lambda *_:self.open_path(self.destination))
        actions.append(self.folder)
        self.choose=Gtk.Button(label='Save to…')
        self.choose.connect('clicked',self.choose_output)
        actions.append(self.choose)
        actions.set_margin_start(24)
        actions.set_margin_end(24)
        actions.set_margin_bottom(12)
        root.append(actions)
        self.output_label=label('Results: '+str(self.output),'footer')
        self.output_label.set_ellipsize(3)
        self.output_label.set_margin_start(24)
        self.output_label.set_margin_end(24)
        self.output_label.set_margin_bottom(12)
        root.append(self.output_label)
        note=label('Recording includes everything visible on the focused monitor.','footer')
        note.set_margin_start(24)
        note.set_margin_end(24)
        note.set_margin_bottom(16)
        root.append(note)
        self.connect('close-request',self.close_requested)
        GLib.timeout_add(150,self.tick)
        self.detect()
        self.detect_cameras()
        self.maximize()
        if '--auto-start' in sys.argv:
            GLib.timeout_add_seconds(2,self.auto_start)

    def detect(self,*_):
        self.start.set_sensitive(False)
        self.refresh.set_sensitive(False)
        def worker():
            try: self.events.put(('gpu',discover()))
            except Exception as error: self.events.put(('discovery_error',str(error)))
        threading.Thread(target=worker,daemon=True).start()

    def auto_start(self):
        if self.gpu and not self.active:
            self.export_options[0].set_active('--record' in sys.argv)
            self.begin()
        return False

    def webcam_toggled(self,*_):
        if self.export_options[3].get_active():
            self.export_options[0].set_active(True)

    def record_toggled(self,*_):
        if not self.export_options[0].get_active():
            self.export_options[3].set_active(False)

    def detect_cameras(self,*_):
        if self.active or self.camera_busy: return
        self.close_preview()
        self.camera_refresh.set_sensitive(False)
        def worker():
            try: self.events.put(('cameras',camera_devices()))
            except Exception as error: self.events.put(('camera_error',str(error)))
        threading.Thread(target=worker,daemon=True).start()

    def close_preview(self):
        if self.camera_preview:
            self.camera_preview.close()
            self.camera_preview=None
        self.preview_button.set_label('Preview camera')

    def camera_changed(self,*_):
        if not self.active and not self.camera_busy: self.close_preview()

    def toggle_preview(self,*_):
        if self.active or self.camera_busy or not self.cameras: return
        if self.camera_preview:
            self.close_preview()
            return
        index=self.camera_select.get_selected()
        if index>=len(self.cameras): return
        device=self.cameras[index]['device']
        size=['Small','Medium','Large'][self.camera_size.get_selected()]
        self.camera_busy=True
        self.preview_button.set_sensitive(False)
        self.start.set_sensitive(False)
        self.camera_refresh.set_sensitive(False)
        self.camera_select.set_sensitive(False)
        self.camera_size.set_sensitive(False)
        def worker():
            preview=CameraPreview(device,size)
            try:
                preview.start()
                self.events.put(('camera_preview',preview))
            except Exception as error:
                self.events.put(('camera_error',str(error)))
        threading.Thread(target=worker,daemon=True).start()

    def append_log(self,text):
        self.buffer.insert(self.buffer.get_end_iter(),text)
        if self.buffer.get_line_count()>2000:
            self.buffer.delete(self.buffer.get_start_iter(),self.buffer.get_iter_at_line(self.buffer.get_line_count()-1600)[1])
        self.logview.scroll_mark_onscreen(self.log_end)

    def begin(self,*_):
        if self.active or self.camera_busy or not self.gpu: return
        use_webcam=self.export_options[3].get_active()
        index=self.camera_select.get_selected()
        if use_webcam and index>=len(self.cameras):
            self.append_log('No camera selected. Refresh cameras or turn off Webcam PiP.\n')
            return
        webcam_device=self.cameras[index]['device'] if use_webcam else None
        webcam_size=['Small','Medium','Large'][self.camera_size.get_selected()]
        self.close_preview()
        self.active=True
        self.pending_cancel=False
        self.runner=None
        try:
            self.inhibit_cookie=self.get_application().inhibit(self,Gtk.ApplicationInhibitFlags.IDLE,'GPU testing and recording in progress')
        except GLib.Error:
            self.inhibit_cookie=0
        self.started=None
        self.samples=[]
        self.buffer.set_text('Checking GPU identity, drivers, and temperature sensors…\n')
        self.status.set_text('TESTING')
        self.stage.set_text('Preparing test')
        self.stage_details.set_text('Five-minute memory check, then Heaven. Shared ten-minute cap.')
        self.start.set_sensitive(False)
        self.stop.set_sensitive(True)
        self.refresh.set_sensitive(False)
        self.choose.set_sensitive(False)
        self.report.set_sensitive(False)
        self.folder.set_sensitive(False)
        for option in self.export_options: option.set_sensitive(False)
        for item in [self.camera_select,self.camera_size,self.camera_refresh,self.preview_button]: item.set_sensitive(False)
        # Re-detect on every click, so a swapped card never inherits stale identity.
        record,reports,overlay,_=[option.get_active() for option in self.export_options]
        def worker():
            try:
                gpu=discover()
                self.events.put(('gpu',gpu))
                self.runner=Runner(gpu,self.output,lambda kind,value:self.events.put((kind,value)),record,reports,overlay=overlay,
                                   webcam_device=webcam_device,webcam_size=webcam_size)
                if self.pending_cancel: self.runner.cancel.set()
                self.runner.run()
            except Exception as error:
                self.events.put(('log','ERROR: '+str(error)+'\n'))
                self.events.put(('done',{'result':{'status':'TOOL ERROR','error':str(error),'warnings':[]},'destination':''}))
        threading.Thread(target=worker,daemon=True).start()

    def cancel_run(self,*_):
        self.pending_cancel=True
        if self.runner: self.runner.cancel.set()
        self.stop.set_sensitive(False)
        self.stage.set_text('Stopping and saving partial evidence…')

    def tick(self):
        if not self.active and self.camera_preview and not self.camera_preview.alive():
            self.close_preview()
            self.append_log('Camera preview ended. Check the camera connection or reopen the preview.\n')
        for _ in range(100):
            try: kind,value=self.events.get_nowait()
            except queue.Empty: break
            if kind=='gpu':
                self.gpu=value
                self.name.set_text(value.info['model'])
                serial=value.info.get('manufacturer_serial') or 'unavailable'
                self.identity.set_text(f'{value.vendor}  ·  {value.info["vram"]}  ·  PCI {value.pci}  ·  Serial: {serial}')
                if not self.active:
                    self.start.set_sensitive(True)
                    self.refresh.set_sensitive(True)
                    self.status.set_text('READY')
            elif kind=='discovery_error':
                self.gpu=None
                self.status.set_text('CHECK GPU')
                self.name.set_text('GPU unavailable')
                self.identity.set_text(value)
                self.append_log(value+'\n')
                self.refresh.set_sensitive(True)
            elif kind=='log': self.append_log(value)
            elif kind=='cameras':
                self.cameras=value
                names=[cam['name'].split(' (usb-',1)[0]+' · '+cam['device'] for cam in value]
                self.camera_select.set_model(Gtk.StringList.new(names or ['No capture camera found']))
                self.camera_select.set_selected(0)
                self.camera_select.set_sensitive(bool(value) and not self.active)
                self.preview_button.set_sensitive(bool(value) and not self.active)
                self.camera_refresh.set_sensitive(not self.active)
                if not value: self.export_options[3].set_active(False)
            elif kind=='camera_preview':
                self.camera_preview=value
                self.camera_busy=False
                self.preview_button.set_label('Close preview')
                self.preview_button.set_sensitive(True)
                self.camera_refresh.set_sensitive(True)
                self.camera_select.set_sensitive(True)
                self.camera_size.set_sensitive(True)
                self.start.set_sensitive(bool(self.gpu))
                self.append_log('Camera preview open. Aim it at the card and monitor; enable Webcam PiP to record it.\n')
            elif kind=='camera_error':
                self.camera_busy=False
                self.camera_refresh.set_sensitive(not self.active)
                for item in [self.camera_select,self.camera_size,self.preview_button]: item.set_sensitive(bool(self.cameras) and not self.active)
                self.start.set_sensitive(bool(self.gpu) and not self.active)
                self.append_log('Camera: '+value+'\n')
            elif kind=='destination':
                self.destination=Path(value)
                self.output_label.set_text('This run: '+value)
            elif kind=='started': self.started=value
            elif kind=='stage':
                self.stage.set_text({'memory':'01 / 02   Testing video memory','graphics':'02 / 02   Running Heaven benchmark','export':'Saving evidence and building reports'}.get(value,value))
            elif kind=='sample':
                self.samples.append(value)
                for key,item in self.metrics.items():
                    data=value.get(key)
                    unit={'core_c':'°C','junction_c':'°C','gpu_load_pct':'%','power_w':' W','vram_used_mib':' GiB'}[key]
                    item.set_text('—' if data is None else (f'{data/1024:.1f}{unit}' if key=='vram_used_mib' else f'{data:.0f}{unit}'))
                self.graph.queue_draw()
            elif kind=='verdict': self.log_note.set_text(value['stage'].capitalize()+': '+value['status'])
            elif kind=='done':
                self.active=False
                if self.inhibit_cookie:
                    self.get_application().uninhibit(self.inhibit_cookie)
                    self.inhibit_cookie=0
                self.status.set_text(value['result']['status'])
                self.stage.set_text('Test complete' if value['result']['status']=='PASS' else 'Run saved · '+value['result']['status'])
                graphics=value['result'].get('tests',{}).get('graphics',{})
                fps=graphics.get('fps')
                self.stage_details.set_text((f'Heaven: {fps:.2f} FPS · ' if fps else '')+'See logs and reports for coverage, settings, and limitations.')
                self.start.set_sensitive(bool(self.gpu))
                self.stop.set_sensitive(False)
                self.refresh.set_sensitive(True)
                self.choose.set_sensitive(True)
                for option in self.export_options: option.set_sensitive(True)
                for item in [self.camera_select,self.camera_size,self.preview_button]: item.set_sensitive(bool(self.cameras))
                self.camera_refresh.set_sensitive(True)
                self.destination=Path(value['destination']) if value['destination'] else None
                self.folder.set_sensitive(bool(self.destination))
                self.report.set_sensitive(bool(self.destination and (self.destination/'GPU-test-report.html').exists()))
                if value['result'].get('warnings'): self.append_log('\nNotes:\n'+'\n'.join(value['result']['warnings'])+'\n')
                elapsed=value['result'].get('testing_and_capture_seconds',0)
                self.timer.set_text(f'{int(elapsed)//60:02d}:{int(elapsed)%60:02d}')
                self.remaining.set_text('Testing finished')
                self.progress.set_fraction(min(elapsed/SESSION_SECONDS,1))
                if '--auto-start' in sys.argv:
                    (APP/'last-validation.json').write_text(json.dumps(value['result'],indent=2))
        if self.active and self.started:
            elapsed=max(0,time.monotonic()-self.started)
            self.timer.set_text(f'{int(elapsed)//60:02d}:{int(elapsed)%60:02d}')
            remain=max(0,int(SESSION_SECONDS-elapsed))
            self.remaining.set_text(f'{remain//60:02d}:{remain%60:02d} test budget left')
            self.progress.set_fraction(min(elapsed/SESSION_SECONDS,1))
        return True

    def draw_graph(self,area,ctx,width,height):
        ctx.set_line_width(1)
        ctx.set_source_rgb(.16,.22,.32)
        for i in (0,1,2):
            y=height*i/2
            ctx.move_to(0,y);ctx.line_to(width,y);ctx.stroke()
        if not self.samples: return
        for key,color in [('core_c',(.35,.59,1)),('junction_c',(.36,.83,.69))]:
            ctx.set_source_rgb(*color)
            ctx.set_line_width(2)
            drawing=False
            for row in self.samples:
                value=row.get(key)
                if value is None: continue
                x=width*row['elapsed_seconds']/SESSION_SECONDS
                y=height*(1-min(value,110)/110)
                if drawing: ctx.line_to(x,y)
                else: ctx.move_to(x,y);drawing=True
            ctx.stroke()

    def choose_output(self,*_):
        dialog=Gtk.FileChooserNative(title='Choose result folder',transient_for=self,action=Gtk.FileChooserAction.SELECT_FOLDER,accept_label='Choose',cancel_label='Cancel')
        def response(chooser,response_id):
            if response_id==Gtk.ResponseType.ACCEPT:
                path=chooser.get_file().get_path()
                if path:
                    self.output=Path(path)
                    self.output_label.set_text('Results: '+path)
            chooser.destroy()
        dialog.connect('response',response)
        dialog.show()

    def open_path(self,path):
        if path:
            subprocess.Popen(['xdg-open',str(path)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)

    def open_report(self,*_):
        if self.destination: self.open_path(self.destination/'GPU-test-report.html')

    def close_requested(self,*_):
        if self.camera_busy:
            self.append_log('Waiting for camera setup to finish; close the preview before exiting.\n')
            return True
        if self.active:
            self.cancel_run()
            self.append_log('Saving partial results before closing. Close again once the run finishes.\n')
            return True
        self.close_preview()
        return False


class Application(Adw.Application):
    def __init__(self):
        super().__init__(application_id='local.omarchy.GpuTester',flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.window=None

    def do_activate(self):
        if not self.window: self.window=Window(self)
        self.window.present()


if __name__=='__main__':
    raise SystemExit(Application().run([sys.argv[0]]))
