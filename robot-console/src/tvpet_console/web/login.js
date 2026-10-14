"use strict";
document.querySelector('#login-form').addEventListener('submit',async e=>{
  e.preventDefault();const button=e.currentTarget.querySelector('button');button.disabled=true;
  try{const response=await fetch('/api/v1/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:'admin',password:document.querySelector('#password').value})});
    const data=await response.json();if(!response.ok)throw new Error(typeof data.detail==='string'?data.detail:'请检查输入');
    document.querySelector('#password').value='';location.replace('/');
  }catch(error){document.querySelector('#login-error').textContent=error.message;}finally{button.disabled=false;}
});
