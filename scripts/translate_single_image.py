from __future__ import annotations

import sys
import os
import asyncio
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QCoreApplication
from PySide6.QtGui import QGuiApplication
from hydra_manga_tl.project.model import MangaProject
from hydra_manga_tl.core.state import APP_STATE
from hydra_manga_tl.core.settings import SETTINGS
from hydra_manga_tl.ocr.runtime import start_ocr_runtime, shutdown_ocr_runtime
from hydra_manga_tl.translation.runtime import TRANSLATION_RUNTIME, shutdown_translation_runtime
from hydra_manga_tl.phase.render_queue import RENDER_QUEUE, shutdown_render_queue
from hydra_manga_tl.translation.requests import RenderRequest

import time
import shutil

def main():
    app = QGuiApplication(sys.argv)
    
    # 1. Setup a dummy project
    project_path = ROOT / "temp_single_image_project"
    if project_path.exists():
        shutil.rmtree(project_path)
    project_path.mkdir(parents=True)
    
    project = MangaProject.create("SingleImageTest", project_path)
    
    # Copy the image
    source_img = Path("C:/Users/PC/.gemini/antigravity-ide/brain/a58c3da2-ac09-4389-ac4a-a15375327ce0/media__1785430581206.png")
    dest_img = project.root_path / source_img.name
    shutil.copy(source_img, dest_img)
    
    project.add_sources([(dest_img, source_img.name)])
    
    # Wait for scan is not needed anymore
    image_record = project.images[0]
    print(f"Loaded image: {image_record.source_path}")

    
    APP_STATE.set_project(project)
    
    from hydra_manga_tl.project.workspace import WorkspaceManager
    from hydra_manga_tl.core.paths import AppPaths
    
    paths = AppPaths(ROOT / "temp_single_image_project_app")
    paths.initialize()
    manager = WorkspaceManager(paths=paths)
    manager._set_current(project)
    
    start_ocr_runtime()
    
    print("Injecting manual region for SFX...")
    # Add manual region
    manager.request_manual_region(0, [10, 10, 120, 209])
    
    # Process events so the manual region service processes it
    for i in range(20):
        app.processEvents()
        time.sleep(0.1)
        
    image = manager.current.images[0]
    if image.manual_regions:
        manual = image.manual_regions[0]
        from hydra_manga_tl.project.editor import RegionEdit
        manager.update_edit(0, manual.id, RegionEdit(
            translated_text="Aah! Aah! Aah!", 
            replace=True, 
            original_text="あ゛ぁ゛♡ あ゛ぁ゛♡ あ゛ぁ゛♡", 
            bubble_type="sfx"
        ))
    else:
        print("Failed to add manual region!")
    
    print("Starting pipeline...")
    if not manager.start_pipeline({image_record.id}):
        print("Failed to start pipeline")
        return
        
    print("Waiting for completion...")
    start_time = time.time()
    
    while manager.pipeline.running:
        app.processEvents()
        time.sleep(0.5)
        if time.time() - start_time > 120:
            print("Timeout waiting for rendering.")
            manager.cancel_pipeline()
            break
            
    if image_record.status in ("ready", "review"):
        print(f"Success! Output saved to: {image_record.rendered_image}")
        out_path = Path("C:/Users/PC/.gemini/antigravity-ide/brain/a58c3da2-ac09-4389-ac4a-a15375327ce0/rendered_output.png")
        shutil.copy(image_record.rendered_image, out_path)
    else:
        print(f"Failed. Error: {image_record.error}")

        
    shutdown_ocr_runtime()
    shutdown_translation_runtime()
    shutdown_render_queue()

if __name__ == "__main__":
    main()
