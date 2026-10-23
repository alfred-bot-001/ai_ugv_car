"use strict";
// Game-style keyboard driving shared by the dashboard and the map page: W/A/S/D hold
// the wheels, the arrow keys hold the gimbal. Held keys add up (W+D drives forward
// while turning right) and the sum is clamped to the joystick's unit circle, so the
// keyboard produces exactly the states a dragged knob would. Pure functions only; the
// pages own the listeners, the held-key set and the control relay.
const KEY_BINDINGS={KeyW:{target:'wheels',x:0,y:1},KeyS:{target:'wheels',x:0,y:-1},KeyA:{target:'wheels',x:-1,y:0},KeyD:{target:'wheels',x:1,y:0},
  ArrowUp:{target:'gimbal',x:0,y:1},ArrowDown:{target:'gimbal',x:0,y:-1},ArrowLeft:{target:'gimbal',x:-1,y:0},ArrowRight:{target:'gimbal',x:1,y:0}};
// The state one target's held keys amount to, or null when none of its keys is down.
function keypadVector(held,target){
  let x=0,y=0,any=false;
  for(const code of held){const binding=KEY_BINDINGS[code];if(!binding||binding.target!==target)continue;any=true;x+=binding.x;y+=binding.y;}
  if(!any)return null;
  const scale=Math.max(1,Math.hypot(x,y));
  return {x:x/scale,y:y/scale};
}
// Keys typed into a field or a dialog never drive the car.
function keypadIgnores(event,document_){
  if(event.repeat||event.altKey||event.ctrlKey||event.metaKey||event.isComposing)return true;
  const element=event.target;
  if(element&&element.tagName&&/^(INPUT|TEXTAREA|SELECT)$/.test(element.tagName))return true;
  if(element&&element.isContentEditable)return true;
  return !!document_.querySelector('dialog[open]');
}
