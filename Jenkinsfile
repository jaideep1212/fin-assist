// Jenkinsfile — defines the CI pipeline as code.
// This starter version just proves Jenkins can pull the repo from GitHub and run.
// We'll add real steps (Python setup, tests) later.

pipeline {
    agent any
    stages {
        stage('Checkout') {
            steps {
                echo 'Checked out fin-assist from GitHub.'
            }
        }
        stage('Set up Python') {
            steps {
                sh 'python3 -m venv venv'
                sh './venv/bin/pip install -r requirements.txt'
            }
        }
        stage('Run tests') {          // <-- the step you're asking about
            steps {
                sh './venv/bin/pytest'
            }
        }
    }
}