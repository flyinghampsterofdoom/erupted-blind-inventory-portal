const form=document.querySelector('#login');
form.addEventListener('submit',async e=>{e.preventDefault();const data=Object.fromEntries(new FormData(form));
 const token=document.cookie.split('; ').find(x=>x.startsWith('csrf_token='))?.split('=').slice(1).join('=');
 const response=await fetch('/login',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':token||''},body:JSON.stringify(data)});
 if(response.ok)location.assign((await response.json()).next||'/');else document.querySelector('#error').textContent=(await response.json()).detail||'Unable to sign in';});
