# ReelSeries-Cast-Converter (protótipo)

Serviço separado para Render, com FFmpeg e FastAPI. Recebe um endereço Xtream autorizado e reempacota MPEG-TS em HLS para o Chromecast. **Não transcodifica codecs** e não garante compatibilidade com todos os canais. Não modifica o backend TMDB existente.

## Implantação futura (não realizada)
1. Crie um repositório **privado** com estes arquivos.
2. Configure um Web Service Docker no Render usando o Dockerfile, inicialmente no plano que escolher (o `render.yaml` é apenas uma referência de configuração, não foi aplicado).
3. Defina `CAST_API_KEY` com uma chave aleatória forte; `XTREAM_ALLOWED_HOSTS` com os domínios exatos dos seus servidores Xtream (separados por vírgula); `PUBLIC_BASE_URL` com a URL HTTPS pública do serviço. Não salve credenciais IPTV no repositório.
4. Confira `/health`. A resposta `configured: true` confirma somente as variáveis, não a compatibilidade do vídeo.
5. O aplicativo Android deverá enviar POST `/api/streams` com `Authorization: Bearer <chave>` e JSON `{"url":"https://provedor-autorizado/live/.../.../123.ts"}`; a resposta inclui `playlist_url`, que será enviado ao Cast como HLS. A URL do stream contém credenciais: use somente HTTPS e nunca registre o corpo da requisição em logs.
6. Use DELETE `/api/streams` ao encerrar. Uma única transmissão simultânea por instância; cada nova solicitação interrompe a anterior. O endpoint HLS tem token aleatório de duração máxima de 30 minutos.

## Limitações de segurança e produção
- **Não coloque a chave administrativa CAST_API_KEY no APK** distribuído: qualquer usuário pode extraí-la. Antes da integração pública, implemente autenticação por usuário (Firebase ID token verificado no backend), autorização e limites de uso por conta.
- A validação de domínio/IP é uma barreira básica contra SSRF; ambientes de produção exigem bloqueio de redirecionamentos, proteção contra DNS rebinding, limites de requisição e observabilidade. O FFmpeg pode seguir redirecionamentos; use somente provedores confiáveis ou um proxy de saída restrito.
- O token na URL HLS é um segredo de acesso temporário. Evite compartilhá-lo ou registrar URLs em logs.
- O disco temporário é efêmero; reinicializações interrompem transmissões. Serviços gratuitos podem suspender após inatividade e podem não ser apropriados para live streaming. Há limites de tráfego e uso; revise os preços antes de produção.
- Alguns servidores Xtream bloqueiam IPs de datacenter; canais também podem exigir transcodificação real (H.264/AAC), que consome mais CPU.
- O protótipo não faz integração com o aplicativo Android; isso é uma etapa posterior.

## Testes locais
```bash
docker build -t reelseries-cast-converter .
docker run --rm -p 10000:10000 -e CAST_API_KEY=uma-chave-segura -e XTREAM_ALLOWED_HOSTS=iptv.example.com -e PUBLIC_BASE_URL=https://seu-servico.onrender.com reelseries-cast-converter
```
