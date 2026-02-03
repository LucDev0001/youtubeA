// Configuração do Firebase
const firebaseConfig = {
  apiKey: "AIzaSyBTvLqiH3yNd8qBivAWZk7DlOwQ-MueROU",
  authDomain: "bot-99528.firebaseapp.com",
  projectId: "bot-99528",
  storageBucket: "bot-99528.firebasestorage.app",
  messagingSenderId: "885510438555",
  appId: "1:885510438555:web:f5cb8f019a15ec5761503e",
};

firebase.initializeApp(firebaseConfig);
const auth = firebase.auth();
const db = firebase.firestore();
