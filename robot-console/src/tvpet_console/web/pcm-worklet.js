/* Bounded 16kHz mono PCM playback with linear resampling to the browser device. */
class PCMPlayer extends AudioWorkletProcessor {
  constructor(){super();this.queue=[];this.position=0;this.current=null;
    this.port.onmessage=e=>{const pcm=new Int16Array(e.data);if(this.queue.length>=8)this.queue.shift();this.queue.push(pcm);};}
  process(inputs,outputs){const output=outputs[0][0];if(!output)return true;
    for(let i=0;i<output.length;i++){
      if(!this.current)this.current=this.queue.shift()||null;
      if(!this.current){output[i]=0;continue;}
      const index=Math.floor(this.position),next=Math.min(index+1,this.current.length-1),fraction=this.position-index;
      output[i]=(this.current[index]*(1-fraction)+this.current[next]*fraction)/32768;
      this.position+=16000/sampleRate;
      if(this.position>=this.current.length){this.position-=this.current.length;this.current=null;}
    }return true;
  }
}
registerProcessor('pcm-player',PCMPlayer);
