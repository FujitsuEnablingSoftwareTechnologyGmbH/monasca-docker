# Monasca CI Docker image

This is a customized jenkins image for use with Monasca CI. See https://github.com/hpcloud-mon/monasca-ci for more details.

## Running/Design
Simply run this Docker container and you will have Jenkins server loaded with some CI jobs for Monasca.
You can run the container, listening on port 8080 with `docker run -d -p 8080:8080 --name jenkins -v /var/run/docker.sock:/var/run/docker.sock monasca/ci`
For a long running Jenkins you may want to setup a persister volume, `-v /your/home:/var/jenkins_home`

After starting the container will load up the standard Jenkins jobs for Monasca CI. These jobs watch all of the Monasca git repos,
if one has a change it will build it then trigger the Monasca job.
  - The main job will build a clustered Monasca setup using other Docker containers and hit it with tests.
    - For this to work the container needs access to the Docker API. For most installations the arguments '-v /var/run/docker.sock:/var/run/docker.sock'
      accomplish this and this is the assumed setup. For some installations it may be necessary to install a key/cert for Docker and set the DOCKER_HOST
      environmental variable. In such cases the jenkins jobs will need to be modified appropriately.
    - A non-clustered version is possible also.

Notes:
  - The integration tests are in a repo that is also built by Jenkins and for which changes triggers new runs.
  - To facilitate the ease of maintenance and setup the system is built entirely with configuration management tools and stored in a git repo.
    All test jobs and code are pulled from various git repos. The Jenkins jobs are built with
    [Jenkins Job Builder](http://docs.openstack.org/infra/jenkins-job-builder/index.html)
  - The Monasca/CI container is built on the Jenkins offical docker repo with Jenkins, plugins, jjb, vagrant and build Monasca dependencies.
    It should only need updates when one of those components is upgraded.
  - Ideally the build artifacts coming from Jenkins should be Docker containers which we start and link for testing. However we don't yet deploy Docker
    in production, so I build the artifacts we do deploy then install and configure each time with the Monasca Ansible roles.
    - There are two images we can use to do this, monasca/ci-base has systemd and ssh running in it but is a bit of a hack as the docker host must
      also be running systemd. The other image is the official ubuntu-upstart:14.04 image.

## Todo
- Document recommended minimum requirements.
- Explore better reporting tools for Jenkins.